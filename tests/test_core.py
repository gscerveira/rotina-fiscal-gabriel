from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import TypeAdapter, ValidationError

from fiscal import core

CFG = core.load_config(Path(__file__).parent.parent / "config.toml")
PERIOD = "2026-09"
REVENUE = Decimal("13731.88")  # fixture for 09/2026 only
TODAY = date(2026, 10, 6)
PAYLOAD: TypeAdapter[core.Payload] = TypeAdapter(core.Payload)


def step(step_id: str) -> core.Step:
    return core.STEP_BY_ID[step_id]


def test_payroll_for_2026_09():
    pay = core.payroll(CFG, PERIOD)
    assert (pay.gross, pay.inss, pay.net) == (Decimal("1621.00"), Decimal("178.31"), Decimal("1442.69"))


def test_2027_is_blocked_until_pro_labore_is_revised():
    with pytest.raises(core.BlockedError) as blocked:
        core.payroll(CFG, "2027-01")
    assert blocked.value.field == "pró-labore"


def test_user_typed_amounts_and_dates():
    assert core.brl(REVENUE) == "13.731,88"
    assert core.BRL_AMOUNT.validate_python("13.731,88") == core.BRL_AMOUNT.validate_python("13731.88") == REVENUE
    assert core.BR_DATE.validate_python("05/10/2026") == date(2026, 10, 5)
    for bad in ("", "abc", "0", "-5", "1,234"):
        with pytest.raises(ValidationError):
            core.BRL_AMOUNT.validate_python(bad)
    with pytest.raises(ValidationError):
        core.BR_DATE.validate_python("31/02/2026")


def test_strict_models_refuse_floats_as_money():
    with pytest.raises(ValidationError):
        core.RevenueConfirmation(period=PERIOD, revenue=13731.88)  # ty: ignore[invalid-argument-type]
    with pytest.raises(ValidationError):
        core.RevenueConfirmation(period="2026-9", revenue=REVENUE)
    with pytest.raises(ValidationError):
        core.shift("2026-13", 1)


def test_config_rejects_overlapping_pro_labore_ranges():
    raw = CFG.model_dump(mode="json")
    raw["pro_labore"].append({"start": "2026-06", "end": "2027-12", "amount": "1700.00"})
    with pytest.raises(ValidationError, match="overlap"):
        core.Config.model_validate(raw, strict=False)


def test_pgdas_foreign_revenue_equals_confirmed_revenue_and_domestic_is_zero():
    p = core.build(step("pgdas"), CFG, PERIOD, REVENUE, None)
    core.check(p, CFG, PERIOD, TODAY)
    assert isinstance(p, core.PgdasDeclaration)
    assert (p.foreign_revenue, p.domestic_revenue) == (REVENUE, Decimal("0.00"))
    assert "Anexo III" in p.classification


def test_pgdas_cannot_be_built_without_confirmed_revenue():
    with pytest.raises(core.BlockedError):
        core.build(step("pgdas"), CFG, PERIOD, None, None)


def test_pgdas_revenue_must_add_up():
    p = core.build(step("pgdas"), CFG, PERIOD, REVENUE, None)
    with pytest.raises(ValidationError) as invalid:
        core.PgdasDeclaration.model_validate(p.model_dump() | {"foreign_revenue": Decimal("1.00")})
    blocked = core.BlockedError.from_validation(invalid.value, "PGDAS-D")
    assert (blocked.field, blocked.expected, blocked.found) == ("receita da atividade", "1.00", "13731.88")


def test_s1200_needs_both_rubrics_and_a_derived_inss():
    p = core.build(step("s1200"), CFG, PERIOD, None, None)
    core.check(p, CFG, PERIOD, TODAY)
    assert isinstance(p, core.S1200)
    pro_labore, inss = p.rubrics
    assert (inss.code, inss.quantity, inss.factor, inss.amount) == ("1950", Decimal("11.00"), None, Decimal("178.31"))

    with pytest.raises(core.BlockedError) as blocked:
        core.check(p.model_copy(update={"rubrics": (pro_labore,)}), CFG, PERIOD, TODAY)
    assert "1950" in blocked.value.field

    wrong = inss.model_copy(update={"amount": Decimal("178.30")})
    with pytest.raises(core.BlockedError) as blocked:
        core.check(p.model_copy(update={"rubrics": (pro_labore, wrong)}), CFG, PERIOD, TODAY)
    assert (blocked.value.expected, blocked.value.found) == ("178.31", "178.30")

    eleven_as_factor = inss.model_copy(update={"factor": Decimal("11.00")})
    with pytest.raises(core.BlockedError) as blocked:
        core.check(p.model_copy(update={"rubrics": (pro_labore, eleven_as_factor)}), CFG, PERIOD, TODAY)
    assert blocked.value.field == "fator do INSS"


def test_s1210_never_invents_the_payment_date():
    with pytest.raises(core.BlockedError):
        core.build(step("s1210"), CFG, PERIOD, None, None)
    p = core.build(step("s1210"), CFG, PERIOD, None, date(2026, 10, 5))
    assert isinstance(p, core.S1210)
    assert p.net == Decimal("1442.69")
    with pytest.raises(core.BlockedError):
        core.check(core.build(step("s1210"), CFG, PERIOD, None, date(2026, 10, 7)), CFG, PERIOD, TODAY)


def test_payloads_round_trip_through_json_by_discriminator():
    for s in core.STEPS:
        p = core.build(s, CFG, PERIOD, REVENUE, date(2026, 10, 5))
        assert PAYLOAD.validate_json(p.model_dump_json()) == p


def test_order_and_resume():
    first = core.next_step({})
    assert first is not None
    assert first.id == "invoices"
    # NFS-e, PGDAS and DAS done: continue straight at eSocial, never repeating PGDAS.
    done = dict.fromkeys(("invoices", "pgdas", "das"), core.Status.DONE)
    resumed = core.next_step(done)
    assert resumed is not None
    assert resumed.id == "s1200"
    assert not core.is_unlocked(step("s1299"), done)
    assert core.next_step(dict.fromkeys(core.STEP_BY_ID, core.Status.DONE)) is None


def test_due_dates():
    assert core.due_date(PERIOD, 15) == date(2026, 10, 15)
    assert core.due_date("2026-12", 20) == date(2027, 1, 20)
    assert core.shift("2026-01", -1) == "2025-12"


def test_client_follows_the_official_layout_rules():
    base = {
        "name": "Acme",
        "country": "it",
        "postal_code": "000",
        "city": "Lecce",
        "region": "000",
        "street": "via Roma",
        "number": "5",
        "district": "Centro",
        "currency": "978",
    }
    client = core.Client.model_validate(base | {"tax_id": "  ", "complement": ""})
    assert (client.country, client.tax_id, client.no_tax_id_reason, client.complement) == ("IT", None, "2", None)
    assert core.Client.model_validate(base | {"tax_id": "IT123"}).no_tax_id_reason is None
    for bad in (
        {"country": "BR"},
        {"country": "ITA"},
        {"currency": "840"},
        {"postal_code": "x" * 12},
        {"name": " "},
        {"tax_id": "IT123", "no_tax_id_reason": "2"},
    ):
        with pytest.raises(ValidationError):
            core.Client.model_validate(base | bad)


def test_invoice_form_reports_every_problem_in_portuguese():
    typed = {"amount": "abc", "foreign_amount": "1,234", "date": "07/10/2026", "description": " "}
    with pytest.raises(ValidationError) as invalid:
        core.InvoiceDraft.model_validate(typed, context={"today": TODAY})
    problems = {str(e["loc"][0]): core.message(e) for e in invalid.value.errors()}
    assert problems == {
        "amount": "use o formato 13.731,88",
        "foreign_amount": "use no máximo duas casas decimais",
        "date": "a competência não pode estar no futuro",
        "description": "preencha este campo",
    }
