import base64
import hashlib
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from lxml import etree
from pydantic import ValidationError

from fiscal import certificate, core, dps
from tests import nfse_samples as samples
from tests.test_certificate import PASSWORD, make_pfx

CFG = core.load_config(Path(__file__).parent.parent / "config.toml")
MODEL = samples.full_nfse_xml()
CLIENT = dps.client_from_model(MODEL, CFG)
DS = "{http://www.w3.org/2000/09/xmldsig#}"
N = {"n": samples.NS}


def draft(**changes: object) -> dps.DpsDraft:
    fields: dict[str, object] = {
        "production": False,
        "number": 5,
        "issued_at": datetime(2026, 10, 7, 13, 0, tzinfo=UTC),
        "service_date": date(2026, 10, 7),
        "description": "Serviços de outubro <ok> & cia",
        "amount": Decimal("9200.00"),
        "foreign_amount": Decimal("1700.50"),
    }
    return dps.DpsDraft.model_validate(fields | changes)


@pytest.fixture
def loaded(tmp_path: Path) -> certificate.Certificate:
    return certificate.load(make_pfx(tmp_path, f"ACME LTDA:{CFG.cnpj}"), PASSWORD, CFG.cnpj, date(2026, 10, 7))


def test_template_and_client_are_read_from_the_model():
    template = dps.template(MODEL, CFG)
    assert (template.series, template.last_dps_number, template.nbs_code) == ("1", 4, "118061000")
    assert (CLIENT.name, CLIENT.country, CLIENT.tax_id, CLIENT.no_tax_id_reason, CLIENT.currency) == (
        "Acme Research Foundation",
        "IT",
        None,
        "2",
        "220",
    )


def test_another_client_changes_the_client_block_currency_and_result_country(loaded: certificate.Certificate):
    german = core.Client(
        name="Beispiel GmbH",
        country="DE",
        tax_id="DE123456789",
        postal_code="10115",
        city="Berlin",
        region="Berlin",
        street="Unter den Linden",
        number="1",
        complement="3. OG",
        district="Mitte",
        currency="978",
    )
    built = dps.build(MODEL, draft(), german, CFG)
    read = {
        name: built.findtext(f"n:infDPS/{path}", namespaces=N)
        for name, path in {
            "tax_id": "n:toma/n:NIF",
            "no_reason": "n:toma/n:cNaoNIF",
            "name": "n:toma/n:xNome",
            "country": "n:toma/n:end/n:endExt/n:cPais",
            "complement": "n:toma/n:end/n:xCpl",
            "currency": "n:serv/n:comExt/n:tpMoeda",
            "result": "n:valores/n:trib/n:tribMun/n:cPaisResult",
        }.items()
    }
    assert read == {
        "tax_id": "DE123456789",
        "no_reason": None,
        "name": "Beispiel GmbH",
        "country": "DE",
        "complement": "3. OG",
        "currency": "978",
        "result": "DE",
    }
    dps.validate(dps.sign(built, loaded))  # element order and sizes hold under the official schema


def test_build_replaces_only_the_per_invoice_fields():
    built = dps.build(MODEL, draft(), CLIENT, CFG)
    info = built.find("n:infDPS", N)
    assert info is not None
    assert info.get("Id") == f"DPS21113002{CFG.cnpj}00001000000000000005"
    changed = {
        "tpAmb": "2",
        "dhEmi": "2026-10-07T13:00:00+00:00",
        "nDPS": "5",
        "dCompet": "2026-10-07",
        "xDescServ": "Serviços de outubro <ok> & cia",
        "vServMoeda": "1700.50",
        "vServ": "9200.00",
    }
    original = etree.fromstring(MODEL).find(".//n:infDPS", N)
    assert original is not None
    municipal = original.find("n:prest/n:IM", N)  # dropped in the test environment only
    assert municipal is not None
    assert info.find("n:prest/n:IM", N) is None
    original.find("n:prest", N).remove(municipal)  # ty: ignore[unresolved-attribute]
    assert (
        dps.build(MODEL, draft(production=True), CLIENT, CFG).findtext("n:infDPS/n:prest/n:IM", namespaces=N)
        == "1234567890"
    )
    for new, old in zip(info.iter(), original.iter(), strict=True):
        name = etree.QName(new).localname
        assert (new.text or "").strip() == changed.get(name, (old.text or "").strip()), name
    assert built.find(f"{DS}Signature") is None


def test_signed_dps_is_schema_valid_and_verifies(loaded: certificate.Certificate):
    signed = dps.sign(dps.build(MODEL, draft(), CLIENT, CFG), loaded)
    dps.validate(signed)
    root = etree.fromstring(signed)
    info, signature = root
    assert signature.tag == f"{DS}Signature"
    assert signature.prefix is None
    # Verify independently of the signing library: digest over infDPS, RSA over SignedInfo.
    digest = hashlib.sha256(etree.tostring(info, method="c14n", exclusive=True, with_comments=True)).digest()
    assert base64.b64encode(digest).decode() == signature.findtext(f".//{DS}DigestValue")
    signed_info = signature.find(f"{DS}SignedInfo")
    assert signed_info is not None
    loaded.key.public_key().verify(
        base64.b64decode(signature.findtext(f"{DS}SignatureValue") or ""),
        etree.tostring(signed_info, method="c14n", exclusive=True, with_comments=True),
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    embedded = base64.b64decode(signature.findtext(f".//{DS}X509Certificate") or "")
    assert embedded == loaded.cert_der()


def test_schema_refuses_a_malformed_dps(loaded: certificate.Certificate):
    broken = dps.build(MODEL, draft(), CLIENT, CFG)
    target = broken.find("n:infDPS/n:valores/n:vServPrest/n:vServ", N)
    assert target is not None
    target.text = "9.200,00"
    with pytest.raises(core.BlockedError) as blocked:
        dps.validate(dps.sign(broken, loaded))
    assert blocked.value.field == "esquema oficial da DPS"


def test_a_model_from_another_company_or_profile_is_refused():
    with pytest.raises(core.BlockedError) as blocked:
        dps.build(samples.full_nfse_xml(issuer=samples.OTHER_CNPJ), draft(), CLIENT, CFG)
    assert blocked.value.field == "prestador da nota modelo"
    domestic = MODEL.replace(b"<tribISSQN>3</tribISSQN>", b"<tribISSQN>1</tribISSQN>")
    with pytest.raises(core.BlockedError) as blocked:
        dps.build(domestic, draft(), CLIENT, CFG)
    assert blocked.value.field == "tributação da nota modelo"


def test_draft_refuses_a_service_date_after_the_issue_time():
    with pytest.raises(ValidationError):
        draft(service_date=date(2026, 10, 8))
    with pytest.raises(ValidationError):
        draft(amount=9200.0)
