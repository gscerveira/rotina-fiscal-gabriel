from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from fiscal import core, db

CFG = core.load_config(Path(__file__).parent.parent / "config.toml")
PERIOD = "2026-09"


def test_migrations_match_the_tables():
    """Fails when a table changes without a new Alembic revision."""
    session = db.open_db()
    context = MigrationContext.configure(session.connection(), opts={"render_as_batch": True})
    assert compare_metadata(context, db.Base.metadata) == []


def test_every_change_leaves_an_audit_entry():
    s = db.open_db()
    payload = core.build(core.STEP_BY_ID["pgdas"], CFG, PERIOD, Decimal("13731.88"), None)
    db.save_input(s, PERIOD, "revenue", "13731.88")
    db.mark(s, PERIOD, "pgdas", core.Status.SENDING, payload)
    db.mark(s, PERIOD, "pgdas", core.Status.DONE, receipt="R-1")

    record = db.steps(s, PERIOD)["pgdas"]
    assert (record.status, record.receipt, record.payload) == (core.Status.DONE, "R-1", payload)
    assert db.inputs(s, PERIOD).revenue == Decimal("13731.88")
    assert [e.action for e in db.audit_trail(s, PERIOD)] == ["USER_CONFIRMED", "SENDING", "DONE"]


def test_malformed_input_never_reaches_the_database():
    s = db.open_db()
    with pytest.raises(ValidationError):
        db.save_input(s, PERIOD, "revenue", "-1")
    with pytest.raises(ValidationError):
        db.save_input(s, "2026-9", "paid_on", "2026-10-05")
    assert db.audit_trail(s, PERIOD) == []


def test_database_itself_refuses_an_unknown_status():
    s = db.open_db()
    s.add(db.StepRow(period=PERIOD, step="pgdas", status="MAYBE", updated_at=db._now()))
    with pytest.raises(IntegrityError):
        s.commit()


def test_invoices_round_trip():
    s = db.open_db()
    invoice = db.Invoice(
        key="k1",
        period=PERIOD,
        number="0040",
        date=date(2026, 9, 29),
        client="Acme Inc (US)",
        amount=Decimal("13731.88"),
        description="Pagamento referente a serviços prestados em Agosto de 2026",
        status="ISSUED",
    )
    db.save_invoice(s, invoice)
    assert db.invoices(s, PERIOD) == [invoice]
