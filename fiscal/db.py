"""Local state: SQLAlchemy over SQLite, schema versioned by Alembic.

Tables store; pydantic models verify. Every row read goes through a model before reaching the
rest of the program, and the audit table is insert-only.
"""

import hashlib
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Any, Literal

from alembic import command
from alembic.config import Config as AlembicConfig
from pydantic import Json, StringConstraints
from sqlalchemy import CheckConstraint, Connection, MetaData, String, create_engine, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column
from sqlalchemy.pool import StaticPool

from .core import BlockedError, Client, Model, NonEmpty, Payload, Period, PositiveMoney, Status

DATA_DIR = Path(__file__).parent.parent / "data"
DEFAULT_URL = f"sqlite:///{DATA_DIR / 'fiscal.db'}"
MIGRATIONS = Path(__file__).parent / "migrations"

# --- Tables -----------------------------------------------------------------


class Base(DeclarativeBase):
    # Explicit constraint names keep Alembic migrations deterministic.
    metadata = MetaData(
        naming_convention={
            "ix": "ix_%(column_0_label)s",
            "uq": "uq_%(table_name)s_%(column_0_name)s",
            "ck": "ck_%(table_name)s_%(constraint_name)s",
            "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
            "pk": "pk_%(table_name)s",
        }
    )


class PeriodInputsRow(Base):
    __tablename__ = "period_inputs"
    period: Mapped[str] = mapped_column(String(7), primary_key=True)
    revenue: Mapped[str | None]  # money as text: SQLite has no exact decimal
    paid_on: Mapped[date | None]


class StepRow(Base):
    __tablename__ = "step"
    __table_args__ = (CheckConstraint(f"status IN ({', '.join(repr(s.value) for s in Status)})", name="status_valid"),)
    period: Mapped[str] = mapped_column(String(7), primary_key=True)
    step: Mapped[str] = mapped_column(primary_key=True)
    status: Mapped[str]
    payload: Mapped[str | None]
    receipt: Mapped[str | None]
    updated_at: Mapped[datetime]


class InvoiceRow(Base):
    __tablename__ = "invoice"
    key: Mapped[str] = mapped_column(primary_key=True)
    period: Mapped[str] = mapped_column(String(7), index=True)
    number: Mapped[str]
    date: Mapped[date]
    client: Mapped[str]
    amount: Mapped[str]
    description: Mapped[str]
    status: Mapped[str]


class AuditRow(Base):
    __tablename__ = "audit"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    at: Mapped[datetime]
    period: Mapped[str] = mapped_column(String(7), index=True)
    subject: Mapped[str]
    action: Mapped[str]
    detail: Mapped[str]


class DocumentRow(Base):
    __tablename__ = "document"
    path: Mapped[str] = mapped_column(primary_key=True)  # relative to the data directory
    period: Mapped[str] = mapped_column(String(7), index=True)
    kind: Mapped[str]
    sha256: Mapped[str] = mapped_column(String(64))
    origin: Mapped[str]
    amount: Mapped[str | None]
    saved_at: Mapped[datetime]


class ClientRow(Base):
    __tablename__ = "client"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(unique=True)
    country: Mapped[str] = mapped_column(String(2))
    tax_id: Mapped[str | None]
    no_tax_id_reason: Mapped[str | None]
    postal_code: Mapped[str]
    city: Mapped[str]
    region: Mapped[str]
    street: Mapped[str]
    number: Mapped[str]
    complement: Mapped[str | None]
    district: Mapped[str]
    currency: Mapped[str] = mapped_column(String(3))
    contract_amount: Mapped[str | None]


class SyncStateRow(Base):
    __tablename__ = "sync_state"
    name: Mapped[str] = mapped_column(primary_key=True)
    value: Mapped[int]


# --- Models for what comes out of the database ------------------------------


class PeriodInputs(Model):
    """What only the user can tell us about a period."""

    period: Period
    revenue: PositiveMoney | None = None
    paid_on: date | None = None


class StepRecord(Model):
    period: Period
    step: NonEmpty
    status: Status
    payload: Json[Payload] | None = None
    receipt: str | None = None
    updated_at: datetime


class Invoice(Model):
    key: NonEmpty
    period: Period
    number: NonEmpty
    date: date
    client: NonEmpty
    amount: PositiveMoney
    description: NonEmpty
    status: Literal["ISSUED", "CANCELLED", "REVIEW"]  # REVIEW: an event we do not interpret was registered


class Document(Model):
    """A file kept under the data directory, with enough metadata to prove it was not altered."""

    path: NonEmpty
    period: Period
    kind: NonEmpty
    sha256: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    origin: NonEmpty
    amount: PositiveMoney | None = None
    saved_at: datetime


class StoredClient(Client):
    id: int


class AuditEvent(Model):
    id: int
    at: datetime
    period: Period
    subject: NonEmpty
    action: NonEmpty
    detail: Json[dict[str, Any]]


def _read[M: Model](model: type[M], row: Base) -> M:
    # Boundary: the database hands back text, so coercion is allowed here.
    return model.model_validate(row, from_attributes=True, strict=False)


# --- Opening and migrating --------------------------------------------------


def migrate(connection: Connection) -> None:
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    cfg.attributes["connection"] = connection
    command.upgrade(cfg, "head")


def open_db(path: Path | None = None) -> Session:
    """Open the database at the latest schema. Without a path, in memory."""
    if path is None:
        engine = create_engine("sqlite://", poolclass=StaticPool)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        migrate(connection)
    return Session(engine)


# --- Writes (each function is one transaction) ------------------------------


class _StepTrace(Model):
    payload: Payload | None
    receipt: str | None


class _InputTrace(Model):
    value: str


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


def _audit(s: Session, period: str, subject: str, action: str, detail: Model) -> None:
    s.add(AuditRow(at=_now(), period=period, subject=subject, action=action, detail=detail.model_dump_json()))


def mark(
    s: Session, period: str, step: str, status: Status, payload: Payload | None = None, receipt: str | None = None
) -> None:
    """Record a step's status and its audit entry in the same transaction."""
    try:
        row = s.get(StepRow, (period, step)) or StepRow(period=period, step=step)
        row.status = status.value
        row.updated_at = _now()
        if payload is not None:
            row.payload = payload.model_dump_json()
        if receipt is not None:
            row.receipt = receipt
        s.add(row)
        _audit(s, period, step, status.value, _StepTrace(payload=payload, receipt=receipt))
        s.commit()
    except BaseException:
        s.rollback()
        raise


def save_input(s: Session, period: str, field: Literal["revenue", "paid_on"], value: str) -> None:
    # Reject a malformed value before touching the database.
    checked = PeriodInputs.model_validate({"period": period, field: value}, strict=False)
    try:
        row = s.get(PeriodInputsRow, period) or PeriodInputsRow(period=period)
        if field == "revenue":
            row.revenue = str(checked.revenue)
        else:
            row.paid_on = checked.paid_on
        s.add(row)
        _audit(s, period, field, "USER_CONFIRMED", _InputTrace(value=value))
        s.commit()
    except BaseException:
        s.rollback()
        raise


def save_invoice(s: Session, invoice: Invoice) -> None:
    try:
        s.merge(InvoiceRow(**invoice.model_dump(exclude={"amount"}), amount=str(invoice.amount)))
        _audit(s, invoice.period, "nfse", invoice.status, invoice)
        s.commit()
    except BaseException:
        s.rollback()
        raise


def set_invoice_status(s: Session, key: str, status: Literal["CANCELLED", "REVIEW"], event: str) -> bool:
    """Apply a government event to one of our invoices. False when the invoice is not ours."""
    row = s.get(InvoiceRow, key)
    if row is None:
        return False
    try:
        if row.status != "CANCELLED":  # a cancellation is final
            row.status = status
        _audit(s, row.period, "nfse", status, _InputTrace(value=f"{row.number}: {event}"))
        s.commit()
    except BaseException:
        s.rollback()
        raise
    return True


def save_document(
    s: Session,
    root: Path,
    relative: str,
    content: bytes,
    *,
    period: str,
    kind: str,
    origin: str,
    amount: Decimal | None = None,
) -> Document:
    """Write a file under `root` and register its hash. An existing file is never overwritten with other content."""
    digest = hashlib.sha256(content).hexdigest()
    known = s.get(DocumentRow, relative)
    target = root / relative
    if known is not None and target.is_file():
        if known.sha256 != digest:
            raise BlockedError("documento " + relative, known.sha256[:12], digest[:12], "Arquivo de documentos")
        return _read(Document, known)
    document = Document(
        path=relative, period=period, kind=kind, sha256=digest, origin=origin, amount=amount, saved_at=_now()
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    try:
        s.merge(DocumentRow(**document.model_dump(exclude={"amount"}), amount=str(amount) if amount else None))
        _audit(s, period, kind, "DOCUMENT_SAVED", document)
        s.commit()
    except BaseException:
        s.rollback()
        raise
    return document


def save_client(s: Session, client: Client) -> StoredClient:
    """Register a client. A name already registered is refused rather than silently replaced."""
    if s.scalars(select(ClientRow).where(ClientRow.name == client.name)).first() is not None:
        raise BlockedError("tomador", "nome ainda não cadastrado", client.name, "Cadastro de tomadores")
    row = ClientRow(**client.model_dump(mode="json"))
    try:
        s.add(row)
        _audit(s, _now().strftime("%Y-%m"), "client", "REGISTERED", client)
        s.commit()
    except BaseException:
        s.rollback()
        raise
    return _read(StoredClient, row)


def update_client(s: Session, client_id: int, client: Client) -> StoredClient:
    row = s.get(ClientRow, client_id)
    if row is None:
        raise BlockedError("tomador", f"cadastro {client_id}", "não encontrado", "Cadastro de tomadores")
    try:
        for field, value in client.model_dump(mode="json").items():
            setattr(row, field, value)
        _audit(s, _now().strftime("%Y-%m"), "client", "UPDATED", client)
        s.commit()
    except BaseException:
        s.rollback()
        raise
    return _read(StoredClient, row)


def set_state(s: Session, name: str, value: int) -> None:
    s.merge(SyncStateRow(name=name, value=value))
    s.commit()


def get_state(s: Session, name: str) -> int:
    row = s.get(SyncStateRow, name)
    return row.value if row else 0


# --- Reads ------------------------------------------------------------------


def steps(s: Session, period: str) -> dict[str, StepRecord]:
    rows = s.scalars(select(StepRow).where(StepRow.period == period))
    return {r.step: _read(StepRecord, r) for r in rows}


def inputs(s: Session, period: str) -> PeriodInputs:
    row = s.get(PeriodInputsRow, period)
    return _read(PeriodInputs, row) if row else PeriodInputs(period=period)


def invoices(s: Session, period: str) -> list[Invoice]:
    rows = s.scalars(select(InvoiceRow).where(InvoiceRow.period == period).order_by(InvoiceRow.date, InvoiceRow.number))
    return [_read(Invoice, r) for r in rows]


def clients(s: Session) -> list[StoredClient]:
    return [_read(StoredClient, r) for r in s.scalars(select(ClientRow).order_by(ClientRow.name))]


def latest_invoice(s: Session) -> Invoice | None:
    """The most recent invoice still standing: the model for the next one."""
    row = s.scalars(
        select(InvoiceRow).where(InvoiceRow.status == "ISSUED").order_by(InvoiceRow.date.desc(), InvoiceRow.key.desc())
    ).first()
    return _read(Invoice, row) if row else None


def documents(s: Session, period: str) -> list[Document]:
    rows = s.scalars(select(DocumentRow).where(DocumentRow.period == period).order_by(DocumentRow.path))
    return [_read(Document, r) for r in rows]


def audit_trail(s: Session, period: str) -> list[AuditEvent]:
    rows = s.scalars(select(AuditRow).where(AuditRow.period == period).order_by(AuditRow.id))
    return [_read(AuditEvent, r) for r in rows]
