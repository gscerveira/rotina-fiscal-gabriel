"""Pure rules of the monthly tax routine: types, pro-labore validity, steps and checks.

No network, no database. Every piece of data is a frozen, strict pydantic model, so nothing is
coerced silently inside the program (a float never becomes money). Lenient coercion happens
only at the boundaries, explicitly: the config file, database rows and what the user types.

Code is English; strings the user reads are Portuguese.
"""

import calendar
import tomllib
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StringConstraints,
    TypeAdapter,
    ValidationError,
    ValidationInfo,
    computed_field,
    field_validator,
    model_validator,
    validate_call,
)
from pydantic_core import ErrorDetails, PydanticCustomError

CENT = Decimal("0.01")


def money(value: Decimal | str) -> Decimal:
    return Decimal(value).quantize(CENT, ROUND_HALF_UP)


def brl(value: Decimal) -> str:
    """13731.88 -> '13.731,88'."""
    return f"{value:,.2f}".translate(str.maketrans(",.", ".,"))


# --- Domain types -----------------------------------------------------------

Period = Annotated[str, StringConstraints(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")]  # competência, "YYYY-MM"
NonEmpty = Annotated[str, StringConstraints(min_length=1, strip_whitespace=True)]
Money = Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=2)]
PositiveMoney = Annotated[Decimal, Field(gt=0, max_digits=12, decimal_places=2)]
Rate = Annotated[Decimal, Field(gt=0, lt=1)]
StepId = Literal["invoices", "pgdas", "das", "s1200", "s1210", "s1299", "dctfweb", "darf"]


def _check_cnpj(value: str) -> str:
    """Verify both check digits. Works for numeric and for alphanumeric (2026+) CNPJs alike."""

    def digit(body: str) -> str:
        weights = [*range(len(body) - 7, 1, -1), *range(9, 1, -1)]
        rest = sum((ord(char) - 48) * weight for char, weight in zip(body, weights, strict=True)) % 11
        return "0" if rest < 2 else str(11 - rest)

    first = digit(value[:12])
    if value[12:] != first + digit(value[:12] + first):
        raise PydanticCustomError("cnpj_check_digits", "dígitos verificadores do CNPJ não conferem")
    return value


def _check_cpf(value: str) -> str:
    def digit(body: str) -> str:
        rest = sum(int(char) * weight for char, weight in zip(body, range(len(body) + 1, 1, -1), strict=True)) % 11
        return "0" if rest < 2 else str(11 - rest)

    first = digit(value[:9])
    if value[9:] != first + digit(value[:9] + first) or len(set(value)) == 1:
        raise PydanticCustomError("cpf_check_digits", "dígitos verificadores do CPF não conferem")
    return value


Cpf = Annotated[str, StringConstraints(pattern=r"^\d{11}$"), AfterValidator(_check_cpf)]
Cnpj = Annotated[str, StringConstraints(pattern=r"^[0-9A-Z]{12}\d{2}$"), AfterValidator(_check_cnpj)]


def _parse_brl(value: object) -> object:
    """'13.731,88' or '13731.88' as typed by the user."""
    if not isinstance(value, str):
        return value
    text = value.strip().removeprefix("R$").strip()
    if "," in text:
        text = text.replace(".", "").replace(",", ".")
    try:
        amount = Decimal(text)
    except InvalidOperation:
        raise PydanticCustomError("brl_amount", "use o formato 13.731,88") from None
    if not amount.is_finite():
        return amount  # the type rejects NaN and infinity
    # Never round what the user typed: a third decimal place is left for the type to reject.
    return amount.quantize(CENT) if amount == money(amount) else amount


def _parse_br_date(value: object) -> object:
    """'05/10/2026' as typed by the user."""
    if not isinstance(value, str):
        return value
    try:
        day, month, year = value.strip().split("/")
        return date(int(year), int(month), int(day))
    except ValueError:
        raise PydanticCustomError("br_date", "use o formato dd/mm/aaaa") from None


BrlAmount = Annotated[PositiveMoney, BeforeValidator(_parse_brl)]
BrDate = Annotated[date, BeforeValidator(_parse_br_date)]
BRL_AMOUNT: TypeAdapter[Decimal] = TypeAdapter(BrlAmount)
BR_DATE: TypeAdapter[date] = TypeAdapter(BrDate)


class Status(StrEnum):
    PENDING = "PENDING"
    SENDING = "SENDING"  # intent recorded, receipt not yet
    DONE = "DONE"
    BLOCKED = "BLOCKED"


class Model(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, validate_default=True)


validated = validate_call(config=ConfigDict(strict=True), validate_return=True)


# --- Errors -----------------------------------------------------------------


def mismatch(field: str, expected: object, found: object) -> PydanticCustomError:
    """Invariant violation carrying the three pieces the validation-error screen shows."""
    return PydanticCustomError(
        "mismatch",
        "{field}: esperado {expected}, encontrado {found}",
        {"field": field, "expected": str(expected), "found": str(found)},
    )


_MESSAGES = {
    "missing": "campo obrigatório",
    "string_too_short": "preencha este campo",
    "string_pattern_mismatch": "formato inválido",
    "greater_than": "deve ser maior que zero",
    "greater_than_equal": "não pode ser negativo",
    "less_than": "acima do limite",
    "decimal_max_places": "use no máximo duas casas decimais",
    "decimal_max_digits": "valor grande demais",
    "extra_forbidden": "campo desconhecido",
    "string_too_long": "texto longo demais",
    "string_type": "preencha este campo",
    "literal_error": "escolha uma das opções",
    "date_from_datetime_parsing": "data inválida",
}


def message(error: ErrorDetails) -> str:
    """Portuguese text for a pydantic error; our own custom errors already carry one."""
    return _MESSAGES.get(error["type"], error["msg"])


class BlockedError(Exception):
    """An inconsistency that halts the automation. It is never fixed automatically."""

    def __init__(self, field: str, expected: object, found: object, where: str = "") -> None:
        self.field, self.expected, self.found, self.where = field, str(expected), str(found), where
        super().__init__(f"{field}: expected {expected}, found {found}")

    @classmethod
    def from_validation(cls, error: ValidationError, where: str) -> Self:
        first = error.errors()[0]
        ctx = first.get("ctx", {})
        if first["type"] == "mismatch":
            return cls(ctx["field"], ctx["expected"], ctx["found"], where)
        return cls(".".join(map(str, first["loc"])) or "conteúdo", message(first), repr(first["input"]), where)


# --- Configuration ----------------------------------------------------------


class ProLaboreRange(Model):
    start: Period
    end: Period
    amount: PositiveMoney

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.start > self.end:
            raise ValueError(f"range ends ({self.end}) before it starts ({self.start})")
        return self


class NfseConfig(Model):
    service_code: Annotated[str, StringConstraints(pattern=r"^\d{2}\.\d{2}\.\d{2}$")]
    taxation: NonEmpty


class PgdasConfig(Model):
    activity: NonEmpty
    classification: NonEmpty
    domestic_revenue: Money


class EsocialConfig(Model):
    worker_category: NonEmpty
    tax_unit: NonEmpty
    rubric_table: NonEmpty
    pro_labore_rubric: NonEmpty
    inss_rubric: NonEmpty
    pro_labore_quantity: PositiveMoney
    inss_quantity: PositiveMoney
    inss_rate: Rate
    payslip_id: NonEmpty
    income_tax_mode: NonEmpty
    other_deductions: Money
    immediate_dctfweb: bool
    # The partner as eSocial knows them. Unset until read from an event the government already accepted.
    worker_cpf: Cpf | None = None
    worker_registration: Annotated[str, StringConstraints(min_length=1, max_length=30)] | None = (
        None  # matrícula (S-2300)
    )
    # indSimples: sent only by companies whose S-1000 says classTrib 03; forbidden otherwise.
    simples_indicator: Literal["1", "2", "3"] | None = None

    @model_validator(mode="after")
    def _distinct_rubrics(self) -> Self:
        if self.pro_labore_rubric == self.inss_rubric:
            raise ValueError("the pro-labore and INSS rubrics cannot be the same")
        return self


class Config(Model):
    cnpj: Cnpj
    certificate_path: Path
    tax_regime: NonEmpty
    accounting_basis: NonEmpty
    market: NonEmpty
    nfse: NfseConfig
    pgdas: PgdasConfig
    esocial: EsocialConfig
    pro_labore: Annotated[tuple[ProLaboreRange, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def _ranges_do_not_overlap(self) -> Self:
        for a, b in pairwise(sorted(self.pro_labore, key=lambda r: r.start)):
            if b.start <= a.end:
                raise ValueError(f"pro-labore ranges overlap: {a.start}..{a.end} and {b.start}..{b.end}")
        return self


def load_config(path: Path) -> Config:
    # Boundary: TOML carries money as text, so coercion is allowed here.
    return Config.model_validate(tomllib.loads(path.read_text(encoding="utf-8")), strict=False)


# --- Step payloads: what the user reviews and what the audit trail keeps -----
# `step` discriminates the union, so stored JSON knows what it is.


class RevenueConfirmation(Model):
    step: Literal["invoices"] = "invoices"
    period: Period
    revenue: PositiveMoney


class PgdasDeclaration(Model):
    step: Literal["pgdas"] = "pgdas"
    period: Period
    accounting_basis: NonEmpty
    domestic_revenue: Money
    foreign_revenue: Money
    activity: NonEmpty
    classification: NonEmpty
    activity_revenue: PositiveMoney
    fixed_iss: Money

    @model_validator(mode="after")
    def _revenue_adds_up(self) -> Self:
        total = self.domestic_revenue + self.foreign_revenue
        if total != self.activity_revenue:
            raise mismatch("receita da atividade", total, self.activity_revenue)
        return self


class DasRequest(Model):
    step: Literal["das"] = "das"
    period: Period


class Rubric(Model):
    table: NonEmpty
    code: NonEmpty
    quantity: PositiveMoney
    factor: PositiveMoney | None = None
    amount: PositiveMoney
    income_tax_mode: NonEmpty


class S1200(Model):
    """Remuneration event."""

    step: Literal["s1200"] = "s1200"
    period: Period
    worker_category: NonEmpty
    tax_unit: NonEmpty
    rubrics: Annotated[tuple[Rubric, ...], Field(min_length=1)]

    @field_validator("rubrics")
    @classmethod
    def _no_repeated_rubric(cls, value: tuple[Rubric, ...]) -> tuple[Rubric, ...]:
        codes = [r.code for r in value]
        if len(set(codes)) != len(codes):
            raise mismatch("rubricas", "códigos distintos", ", ".join(codes))
        return value


class S1210(Model):
    """Payment event."""

    step: Literal["s1210"] = "s1210"
    period: Period
    payslip_id: NonEmpty
    paid_on: date
    gross: PositiveMoney
    inss: Money
    other_deductions: Money
    net: PositiveMoney

    @model_validator(mode="after")
    def _net_adds_up(self) -> Self:
        expected = self.gross - self.inss - self.other_deductions
        if self.net != expected:
            raise mismatch("valor líquido", expected, self.net)
        return self


class S1299(Model):
    """Payroll closing event."""

    step: Literal["s1299"] = "s1299"
    period: Period
    has_remuneration: bool
    has_payment: bool
    has_rural_sales: bool
    has_dock_workers: bool
    has_payroll_relief: bool
    immediate_dctfweb: bool


class DctfwebCheck(Model):
    step: Literal["dctfweb", "darf"]
    period: Period
    expected_inss: Money


Payload = Annotated[
    RevenueConfirmation | PgdasDeclaration | DasRequest | S1200 | S1210 | S1299 | DctfwebCheck,
    Field(discriminator="step"),
]


def _blank_as_none(value: object) -> object:
    return None if isinstance(value, str) and not value.strip() else value


def _upper(value: object) -> object:
    return value.strip().upper() if isinstance(value, str) else value


def _sized(maximum: int) -> StringConstraints:
    return StringConstraints(min_length=1, max_length=maximum, strip_whitespace=True)


class Client(Model):
    """A service taker abroad. Field sizes are the official NFS-e layout's (Anexo I, group `toma`)."""

    name: Annotated[str, _sized(150)]
    country: Annotated[str, BeforeValidator(_upper), StringConstraints(pattern=r"^[A-Z]{2}$")]  # ISO 3166 alpha-2
    tax_id: Annotated[Annotated[str, _sized(40)] | None, BeforeValidator(_blank_as_none)] = None  # NIF
    # Why there is no NIF: 1 = exempt from it, 2 = the country does not require one.
    no_tax_id_reason: Literal["1", "2"] | None = None
    postal_code: Annotated[str, _sized(11)]
    city: Annotated[str, _sized(60)]
    region: Annotated[str, _sized(60)]
    street: Annotated[str, _sized(255)]
    number: Annotated[str, _sized(60)]
    complement: Annotated[Annotated[str, _sized(156)] | None, BeforeValidator(_blank_as_none)] = None
    district: Annotated[str, _sized(60)]
    currency: Literal["220", "978"]  # Banco Central currency table: 220 = US dollar, 978 = euro
    # The contract price in that currency, offered as the starting value of each new invoice.
    contract_amount: Annotated[BrlAmount | None, BeforeValidator(_blank_as_none)] = None

    @model_validator(mode="before")
    @classmethod
    def _default_reason(cls, data: object) -> object:
        # A form without a NIF means "the country does not require one" unless told otherwise.
        if isinstance(data, dict) and not str(data.get("tax_id") or "").strip() and not data.get("no_tax_id_reason"):
            return data | {"no_tax_id_reason": "2"}
        return data

    @model_validator(mode="after")
    def _identified_one_way(self) -> Self:
        if self.country == "BR":
            raise PydanticCustomError("domestic_client", "o tomador do exterior não pode ser do Brasil")
        if (self.tax_id is None) == (self.no_tax_id_reason is None):
            raise PydanticCustomError("tax_id_choice", "informe o NIF ou o motivo de não ter, nunca os dois")
        return self


CURRENCIES = {"220": "dólar dos EUA", "978": "euro"}


class InvoiceDraft(Model):
    """The NFS-e form exactly as typed. Validate with context={'today': date}."""

    amount: BrlAmount
    # Mandatory in the official layout (comExt/vServMoeda): the price in the client's currency, as declared.
    foreign_amount: BrlAmount
    date: BrDate
    description: Annotated[str, StringConstraints(min_length=1, max_length=2000, strip_whitespace=True)]

    @field_validator("date")
    @classmethod
    def _not_in_the_future(cls, value: date, info: ValidationInfo) -> date:
        today = (info.context or {}).get("today")
        if today is not None and value > today:
            raise PydanticCustomError("future_date", "a competência não pode estar no futuro")
        return value


# --- Steps ------------------------------------------------------------------


class Step(Model):
    id: StepId
    name: NonEmpty
    target: NonEmpty  # who receives it
    depends_on: StepId | None = None
    due_day: Annotated[int, Field(ge=1, le=28)] | None = None  # day of the month after the period
    stamp: NonEmpty  # shown once the step is done
    transmits: bool = True  # False: a local confirmation, nothing leaves the machine


class Workflow(Model):
    steps: tuple[Step, ...]

    @model_validator(mode="after")
    def _dependencies_come_first(self) -> Self:
        seen: set[str] = set()
        for step in self.steps:
            if step.id in seen:
                raise ValueError(f"repeated step: {step.id}")
            if step.depends_on is not None and step.depends_on not in seen:
                raise ValueError(f"{step.id} depends on {step.depends_on}, which does not come before it")
            seen.add(step.id)
        return self


# Two independent tracks: invoices→pgdas→das and s1200→s1210→s1299→dctfweb→darf.
STEPS = Workflow(
    steps=(
        Step(id="invoices", name="Notas", target="local", stamp="CONFIRMADO", transmits=False),
        Step(id="pgdas", name="PGDAS-D", target="Receita", depends_on="invoices", due_day=20, stamp="TRANSMITIDO"),
        Step(id="das", name="DAS", target="Receita", depends_on="pgdas", due_day=20, stamp="GERADO"),
        Step(id="s1200", name="S-1200", target="eSocial", due_day=15, stamp="TRANSMITIDO"),
        Step(id="s1210", name="S-1210", target="eSocial", depends_on="s1200", due_day=15, stamp="TRANSMITIDO"),
        Step(id="s1299", name="S-1299", target="eSocial", depends_on="s1210", due_day=15, stamp="FECHADO"),
        Step(id="dctfweb", name="DCTFWeb", target="Receita", depends_on="s1299", due_day=20, stamp="CONFERIDA"),
        Step(id="darf", name="DARF", target="Receita", depends_on="dctfweb", due_day=20, stamp="GERADO"),
    )
).steps
STEP_BY_ID: dict[str, Step] = {s.id: s for s in STEPS}


def is_unlocked(step: Step, status: dict[str, Status]) -> bool:
    return step.depends_on is None or status.get(step.depends_on) == Status.DONE


def next_step(status: dict[str, Status]) -> Step | None:
    """First step that is not done and whose dependency is done."""
    return next((s for s in STEPS if status.get(s.id) != Status.DONE and is_unlocked(s, status)), None)


# --- Periods and due dates --------------------------------------------------


@validated
def period_of(day: date) -> Period:
    return f"{day.year:04d}-{day.month:02d}"


@validated
def shift(period: Period, months: int) -> Period:
    year, month = map(int, period.split("-"))
    n = year * 12 + (month - 1) + months
    return f"{n // 12:04d}-{n % 12 + 1:02d}"


@validated
def label(period: Period) -> str:
    """'2026-09' -> '09/2026'."""
    return f"{period[5:]}/{period[:4]}"


@validated
def due_date(period: Period, day: Annotated[int, Field(ge=1, le=31)]) -> date:
    """The given day of the month after the period, clamped to that month's length."""
    year, month = map(int, shift(period, 1).split("-"))
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


# --- Payroll ----------------------------------------------------------------


class Payroll(Model):
    """INSS and net are always derived: a payroll whose numbers do not add up cannot exist."""

    gross: PositiveMoney
    inss_rate: Rate
    other_deductions: Money

    @computed_field
    @property
    def inss(self) -> Decimal:
        return money(self.gross * self.inss_rate)

    @computed_field
    @property
    def net(self) -> Decimal:
        return self.gross - self.inss - self.other_deductions


def pro_labore_for(cfg: Config, period: str) -> Decimal:
    for r in cfg.pro_labore:
        if r.start <= period <= r.end:
            return r.amount
    raise BlockedError(
        "pró-labore",
        f"vigência cobrindo {label(period)}",
        "nenhuma",
        "config.toml: o valor precisa ser revisado; acrescente um bloco [[pro_labore]]",
    )


def payroll(cfg: Config, period: str) -> Payroll:
    es = cfg.esocial
    return Payroll(gross=pro_labore_for(cfg, period), inss_rate=es.inss_rate, other_deductions=es.other_deductions)


# --- Build and check --------------------------------------------------------


def _build(step: Step, cfg: Config, period: str, revenue: Decimal | None, paid_on: date | None) -> Payload:
    es, pg = cfg.esocial, cfg.pgdas
    match step.id:
        case "invoices" | "pgdas" | "das":
            if revenue is None:
                raise BlockedError("faturamento", "confirmado pelo usuário", "ausente", "Notas")
            if step.id == "invoices":
                return RevenueConfirmation(period=period, revenue=revenue)
            if step.id == "das":
                return DasRequest(period=period)
            return PgdasDeclaration(
                period=period,
                accounting_basis=cfg.accounting_basis,
                domestic_revenue=pg.domestic_revenue,
                foreign_revenue=revenue,
                activity=pg.activity,
                classification=pg.classification,
                activity_revenue=revenue,
                fixed_iss=Decimal("0.00"),
            )
        case "s1200":
            pay = payroll(cfg, period)

            def rubric(code: str, quantity: Decimal, amount: Decimal) -> Rubric:
                return Rubric(
                    table=es.rubric_table,
                    code=code,
                    quantity=quantity,
                    amount=amount,
                    income_tax_mode=es.income_tax_mode,
                )

            return S1200(
                period=period,
                worker_category=es.worker_category,
                tax_unit=es.tax_unit,
                rubrics=(
                    rubric(es.pro_labore_rubric, es.pro_labore_quantity, pay.gross),
                    rubric(es.inss_rubric, es.inss_quantity, pay.inss),
                ),
            )
        case "s1210":
            if paid_on is None:
                raise BlockedError("data do pagamento", "informada pelo usuário", "ausente", step.name)
            pay = payroll(cfg, period)
            return S1210(
                period=period,
                payslip_id=es.payslip_id,
                paid_on=paid_on,
                gross=pay.gross,
                inss=pay.inss,
                other_deductions=pay.other_deductions,
                net=pay.net,
            )
        case "s1299":
            return S1299(
                period=period,
                has_remuneration=True,
                has_payment=True,
                has_rural_sales=False,
                has_dock_workers=False,
                has_payroll_relief=False,
                immediate_dctfweb=es.immediate_dctfweb,
            )
        case "dctfweb" | "darf":
            return DctfwebCheck(step=step.id, period=period, expected_inss=payroll(cfg, period).inss)


def build(step: Step, cfg: Config, period: str, revenue: Decimal | None, paid_on: date | None) -> Payload:
    """What the step will send. Every failure, of type or of rule, surfaces as BlockedError."""
    try:
        return _build(step, cfg, period, revenue, paid_on)
    except ValidationError as error:
        raise BlockedError.from_validation(error, step.name) from error


def check(payload: Payload, cfg: Config, period: str, today: date) -> None:
    """Compare a payload against the current configuration. Raises BlockedError on the first mismatch."""
    es = cfg.esocial
    where = STEP_BY_ID[payload.step].name

    def same(field: str, expected: object, found: object) -> None:
        if expected != found:
            raise BlockedError(field, expected, found, where)

    same("competência", period, payload.period)
    match payload:
        case PgdasDeclaration():
            same("mercado interno", cfg.pgdas.domestic_revenue, payload.domestic_revenue)
            same("mercado externo", payload.activity_revenue, payload.foreign_revenue)
            same("atividade", cfg.pgdas.activity, payload.activity)
            same("classificação", cfg.pgdas.classification, payload.classification)
        case S1200():
            pay = payroll(cfg, period)
            by_code = {r.code: r for r in payload.rubrics}
            for code, name in ((es.pro_labore_rubric, "pró-labore"), (es.inss_rubric, "INSS")):
                if code not in by_code:
                    raise BlockedError(f"rubrica {code} ({name})", "presente", "ausente", where)
            pro_labore, inss = by_code[es.pro_labore_rubric], by_code[es.inss_rubric]
            same("categoria", es.worker_category, payload.worker_category)
            same("lotação", es.tax_unit, payload.tax_unit)
            same("tabela de rubricas", es.rubric_table, pro_labore.table)
            same("quantidade do pró-labore", es.pro_labore_quantity, pro_labore.quantity)
            same("valor do pró-labore", pay.gross, pro_labore.amount)
            same("quantidade do INSS", es.inss_quantity, inss.quantity)
            same("fator do INSS", None, inss.factor)
            same("valor do INSS", pay.inss, inss.amount)
        case S1210():
            same("valor líquido", payroll(cfg, period).net, payload.net)
            if payload.paid_on > today:
                raise BlockedError("data do pagamento", f"até {today:%d/%m/%Y}", f"{payload.paid_on:%d/%m/%Y}", where)
        case DctfwebCheck():
            same("INSS esperado", payroll(cfg, period).inss, payload.expected_inss)
        case _:
            pass
