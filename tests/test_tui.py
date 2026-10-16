"""Drive the TUI headlessly. With CAPTURE=1, screenshots land in .impeccable/review/."""

import asyncio
import os
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import cast

from sqlalchemy.orm import Session
from textual.widgets import Button, Input, Select

from fiscal import core, db, nfse
from fiscal.gateway import Gateway
from fiscal.tui import FiscalApp, NewClient

ROOT = Path(__file__).parent.parent
CFG = core.load_config(ROOT / "config.toml")
TODAY = date(2026, 10, 6)
CLIENT = core.Client(
    name="Acme Research Foundation",
    country="IT",
    postal_code="000",
    city="Lecce",
    region="000",
    street="via Roma",
    number="5",
    district="Centro",
    currency="978",
)
SIZE = (90, 34)


def shot(app: FiscalApp, name: str) -> None:
    if os.environ.get("CAPTURE"):
        folder = ROOT / ".impeccable" / "review"
        folder.mkdir(parents=True, exist_ok=True)
        app.save_screenshot(str(folder / f"{name}.svg"))


def seed(s: Session, key: str, number: str, day: date, amount: str) -> None:
    db.save_invoice(
        s,
        db.Invoice(
            key=key,
            period=core.period_of(day),
            number=number,
            date=day,
            client="Acme Inc (US)",
            amount=Decimal(amount),
            description="Pagamento referente a serviços prestados",
            status="ISSUED",
        ),
    )


def test_full_closing_in_simulation():
    s = db.open_db()
    seed(s, "k1", "0040", date(2026, 9, 29), "13731.88")
    seed(s, "k2", "0041", date(2026, 10, 2), "9200.00")

    async def script() -> None:
        app = FiscalApp(s, CFG, today=TODAY, simulate=True)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            shot(app, "home")
            for step in core.STEPS:
                await pilot.press("enter")  # opens the lit step
                await pilot.pause()
                if step.id == "invoices":
                    await pilot.press("enter")  # accepts the revenue found in the invoices
                if step.id == "s1210":
                    await pilot.press(*"05102026", "enter")  # digits only: the slashes type themselves
                await pilot.pause()
                if step.id in ("pgdas", "s1200", "s1210"):
                    shot(app, f"review-{step.id}")
                await pilot.press("enter")  # focus is already on Confirm
                await pilot.pause()
            shot(app, "processed")
            await pilot.press("a")
            await pilot.pause()
            shot(app, "audit")

    asyncio.run(script())
    assert {name: r.status for name, r in db.steps(s, "2026-09").items()} == dict.fromkeys(
        core.STEP_BY_ID, core.Status.DONE
    )
    typed = db.inputs(s, "2026-09")
    assert (typed.revenue, typed.paid_on) == (Decimal("13731.88"), date(2026, 10, 5))


def test_nothing_can_be_transmitted_outside_simulation():
    s = db.open_db()

    async def script() -> None:
        app = FiscalApp(s, CFG, today=TODAY)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.press("down", "down", "down", "enter")  # S-1200 has no dependency
            await pilot.pause()
            shot(app, "channel-not-connected")
            assert app.screen.query_one("#confirm", Button).disabled
            await pilot.press("enter")  # Back has the focus; pressing it must not confirm anything
            await pilot.pause()

    asyncio.run(script())
    assert db.steps(s, "2026-09") == {}


def test_invoice_form_validates_then_issues_in_simulation():
    s = db.open_db()
    db.save_client(s, CLIENT)

    async def script() -> None:
        app = FiscalApp(s, CFG, today=TODAY, simulate=True)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.press("n")
            await pilot.pause()
            # Letters never enter a money field, so this form is reviewed empty.
            await pilot.press("tab", *"abc", "tab", "tab", "tab", "tab", "tab", "enter")
            await pilot.pause()
            shot(app, "invoice-errors")
            assert db.invoices(s, "2026-10") == []
            await pilot.press("shift+tab", "shift+tab", "shift+tab", "shift+tab", "shift+tab")  # back to amount
            await pilot.press(*"410000", "tab", *"78600", "tab")
            await pilot.press(*"06102026", "tab", *"Servicos de outubro", "tab", "tab", "enter")
            await pilot.pause()
            shot(app, "invoice-review")
            await pilot.press("tab", "enter")  # Confirm and issue
            await pilot.pause()
            shot(app, "home-after-invoice")

    asyncio.run(script())
    (issued,) = db.invoices(s, "2026-10")
    assert (issued.client, issued.amount, issued.number) == ("Acme Research Foundation", Decimal("4100.00"), "0001")


def test_2027_is_blocked_on_screen():
    async def script() -> None:
        app = FiscalApp(db.open_db(), CFG, today=date(2027, 2, 3))
        async with app.run_test(size=SIZE) as pilot:
            await pilot.press("down", "down", "down", "enter")
            await pilot.pause()
            shot(app, "blocked-2027")
            assert app.screen.query_one("#confirm", Button).disabled

    asyncio.run(script())


def test_sync_runs_on_start_and_cancelled_invoices_leave_the_suggested_revenue(tmp_path: Path):
    s = db.open_db()
    runs: list[int] = []

    class FakeGateway:
        environment = nfse.Environment.PRODUCTION

        def sync(self) -> nfse.SyncReport:
            return fake_sync()

    def fake_sync() -> nfse.SyncReport:
        if not runs:  # the first run brings two invoices, one of them cancelled later by an event
            seed(s, "k1", "1", date(2026, 9, 29), "13731.88")
            seed(s, "k2", "2", date(2026, 9, 30), "500.00")
            db.set_invoice_status(s, "k2", "CANCELLED", "e101101")
            db.save_document(
                s, tmp_path, "2026/09/NFS-e-1.xml", b"<x/>", period="2026-09", kind="NFS-e XML", origin="ADN"
            )
        runs.append(1)
        return nfse.SyncReport(issued=2 if len(runs) == 1 else 0, events=1 if len(runs) == 1 else 0)

    async def script() -> None:
        app = FiscalApp(s, CFG, today=TODAY, gateway=cast("Gateway", FakeGateway()), data_dir=tmp_path)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            shot(app, "home-synced")
            await pilot.press("s")
            await pilot.pause()
            assert len(runs) == 2
            await pilot.press("d")
            await pilot.pause()
            shot(app, "documents")
            await pilot.press("escape", "enter")  # review the invoices step
            await pilot.pause()
            shot(app, "review-invoices-with-cancelled")
            assert app.screen.query_one(Input).value == "13.731,88"

    asyncio.run(script())


def test_registering_a_client_from_the_invoice_form_selects_it():
    s = db.open_db()
    db.save_client(s, CLIENT)

    async def script() -> None:
        app = FiscalApp(s, CFG, today=TODAY, simulate=True)
        async with app.run_test(size=(90, 40)) as pilot:
            await pilot.press("n")
            await pilot.pause()
            invoice_form = app.screen
            app.push_screen(NewClient())  # what choosing "Cadastrar novo tomador…" does
            await pilot.pause()
            await pilot.press(*["tab"] * 13, "enter")  # through every field to Cadastrar, with the form empty
            await pilot.pause()
            shot(app, "client-errors")
            assert db.clients(s) == [db.StoredClient(id=1, **CLIENT.model_dump())]  # an empty form registers nothing
            for field, text in {
                "name": "Beispiel GmbH",
                "country": "de",
                "tax_id": "DE123456789",
                "street": "Unter den Linden",
                "number": "1",
                "district": "Mitte",
                "city": "Berlin",
                "region": "Berlin",
                "postal_code": "10115",
                "contract_amount": "150000",
            }.items():
                app.screen.query_one(f"#{field}", Input).value = text
            app.screen.query_one("#currency", Select).value = "978"
            await pilot.pause()
            shot(app, "client-form")
            await pilot.pause(0.3)  # a Textual button ignores a second press for 0.2 s after the first
            await pilot.press("enter")  # focus is on Cadastrar
            await pilot.pause()
            assert app.screen is invoice_form
            assert app.screen.query_one("#client", Select).value == 2
            assert app.screen.query_one("#foreign_amount", Input).value == "1.500,00"  # the contract price, editable
            shot(app, "invoice-form-with-new-client")

    asyncio.run(script())
    german = db.clients(s)[1]
    assert german.contract_amount == Decimal("1500.00")
    assert (german.name, german.country, german.tax_id, german.no_tax_id_reason) == (
        "Beispiel GmbH",
        "DE",
        "DE123456789",
        None,
    )
