import base64
import gzip
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from lxml import etree
from sqlalchemy.orm import Session

from fiscal import certificate, core, db, dps, nfse
from fiscal.gateway import LAST_DPS, PENDING_DPS, Gateway
from tests import nfse_samples as samples
from tests.test_certificate import PASSWORD, make_pfx

CFG = core.load_config(Path(__file__).parent.parent / "config.toml")
TODAY = date.today()
N = {"n": samples.NS}


class Sefin:
    """A SEFIN stand-in: wraps each DPS it receives into an NFS-e, or misbehaves on request."""

    def __init__(self, mode: str = "accept", already_issued: bool = False) -> None:
        self.mode, self.already_issued = mode, already_issued
        self.received: list[bytes] = []
        self.calls: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(f"{request.method} {request.url.host}{request.url.path}")
        if request.method == "HEAD":
            return httpx.Response(200 if self.already_issued else 404)
        if request.method == "GET":  # the ADN side: nothing new to sync, PDFs unavailable
            if "/danfse/" in request.url.path:
                return httpx.Response(503)
            return httpx.Response(404, content=samples.batch(environment="HOMOLOGACAO"))
        if self.mode == "timeout":
            raise httpx.ReadTimeout("no answer", request=request)
        sent = gzip.decompress(base64.b64decode(json.loads(request.content)["dpsXmlGZipB64"]))
        self.received.append(sent)
        if self.mode == "reject":
            body = {"erros": [{"Codigo": "E0014", "Descricao": "DPS duplicada"}]}
            return httpx.Response(400, json=body)
        inner = sent.split(b"?>", 1)[1]
        nfse_xml = (
            (
                f'<NFSe xmlns="{samples.NS}" versao="1.01"><infNFSe Id="NFS{samples.key(2)}"><nNFSe>2</nNFSe>'
                f"<cStat>100</cStat><emit><CNPJ>{CFG.cnpj}</CNPJ></emit>"
            ).encode()
            + inner
            + b"</infNFSe></NFSe>"
        )
        return httpx.Response(
            201, json={"chaveAcesso": samples.key(2), "idDps": "DPS", "nfseXmlGZipB64": samples.zipped(nfse_xml)}
        )


@pytest.fixture
def world(tmp_path: Path) -> tuple[Session, Path, certificate.Certificate]:
    """A database holding one issued invoice (the model), its XML on disk, and a test certificate."""
    s = db.open_db()
    model = samples.full_nfse_xml()
    parsed = nfse.parse_nfse(model)
    db.save_invoice(
        s,
        db.Invoice(
            key=parsed.key,
            period=parsed.period,
            number=parsed.number,
            date=parsed.service_date,
            client=parsed.client,
            amount=parsed.amount,
            description=parsed.description,
            status="ISSUED",
        ),
    )
    db.save_document(s, tmp_path, "2026/09/NFS-e-1.xml", model, period="2026-09", kind="NFS-e XML", origin="ADN")
    loaded = certificate.load(make_pfx(tmp_path / "..", f"ACME LTDA:{CFG.cnpj}"), PASSWORD, CFG.cnpj, date(2026, 10, 7))
    return s, tmp_path, loaded


def gateway(world: tuple[Session, Path, certificate.Certificate], sefin: Sefin, production: bool = False) -> Gateway:
    s, root, loaded = world
    return Gateway(
        s,
        CFG,
        environment=nfse.Environment.PRODUCTION if production else nfse.Environment.RESTRICTED,
        data_dir=root,
        certificate_path=root / "unused.pfx",
        loaded=loaded,
        http=httpx.Client(transport=httpx.MockTransport(sefin)),
    )


def typed(**changes: str) -> core.InvoiceDraft:
    fields = {"amount": "9.200,00", "foreign_amount": "1.700,50", "date": f"{TODAY:%d/%m/%Y}", "description": "Outubro"}
    return core.InvoiceDraft.model_validate(fields | changes, context={"today": TODAY})


def test_issue_sends_once_files_everything_and_advances_the_number(world):
    s, _, _ = world
    sefin = Sefin()
    g = gateway(world, sefin)
    preview = g.preview()
    (client,) = g.clients()  # seeded from the model invoice
    assert (preview.number, client.country, client.currency) == (5, "IT", "220")
    assert g.clients() == [client]  # seeding happens once

    invoice = g.issue(typed(), preview.number, client)

    assert len(sefin.received) == 1
    assert sefin.calls == ["POST sefin.producaorestrita.nfse.gov.br/SefinNacional/nfse"]
    sent = etree.fromstring(sefin.received[0])
    assert sent.findtext("n:infDPS/n:tpAmb", namespaces=N) == "2"  # test environment, never production
    assert sent.findtext("n:infDPS/n:valores/n:vServPrest/n:vServ", namespaces=N) == "9200.00"
    dps.validate(sefin.received[0])
    assert (invoice.number, invoice.amount, invoice.status) == ("2", Decimal("9200.00"), "ISSUED")
    kinds = sorted(d.kind for d in db.documents(s, core.period_of(TODAY)))
    assert kinds == ["DPS enviada", "NFS-e XML"]
    assert (db.get_state(s, LAST_DPS), db.get_state(s, PENDING_DPS)) == (5, 0)
    assert g.preview().number == 6


def test_production_gateway_marks_the_dps_as_production(world):
    sefin = Sefin()
    g = gateway(world, sefin, production=True)
    g.issue(typed(), g.preview().number, g.clients()[0])
    assert sefin.calls == ["POST sefin.nfse.gov.br/SefinNacional/nfse"]
    assert b"<tpAmb>1</tpAmb>" in sefin.received[0]


def test_a_rejection_blocks_keeps_what_was_sent_and_frees_the_number(world):
    s, _, _ = world
    g = gateway(world, Sefin("reject"))
    with pytest.raises(core.BlockedError) as blocked:
        g.issue(typed(), g.preview().number, g.clients()[0])
    assert "E0014 DPS duplicada" in blocked.value.found
    assert [d.kind for d in db.documents(s, core.period_of(TODAY))] == ["DPS enviada"]
    assert db.invoices(s, core.period_of(TODAY)) == []
    assert (db.get_state(s, PENDING_DPS), g.preview().number) == (0, 5)


def test_a_lost_answer_is_never_resent_blindly(world):
    s, _, _ = world
    lost = gateway(world, Sefin("timeout"))
    with pytest.raises(core.BlockedError) as blocked:
        lost.issue(typed(), 5, lost.clients()[0])
    assert blocked.value.field == "envio da DPS"
    assert db.get_state(s, PENDING_DPS) == 5

    # The government says the DPS did become an NFS-e: refuse to issue again until the invoices are synced.
    arrived = Sefin(already_issued=True)
    with pytest.raises(core.BlockedError) as blocked:
        gateway(world, arrived).preview()
    assert "já virou NFS-e" in blocked.value.found
    assert arrived.calls[0].startswith("HEAD ")
    assert (db.get_state(s, PENDING_DPS), db.get_state(s, LAST_DPS)) == (0, 5)


def test_a_lost_send_that_never_arrived_frees_the_number(world):
    s, _, _ = world
    failing = gateway(world, Sefin("timeout"))
    client = failing.clients()[0]
    with pytest.raises(core.BlockedError):
        failing.issue(typed(), 5, client)
    assert gateway(world, Sefin(already_issued=False)).preview().number == 5
    assert db.get_state(s, PENDING_DPS) == 0


def test_no_model_invoice_blocks():
    g = Gateway(
        db.open_db(),
        CFG,
        environment=nfse.Environment.RESTRICTED,
        data_dir=Path("/nonexistent"),
        certificate_path=Path("x"),
        http=httpx.Client(transport=httpx.MockTransport(Sefin())),
    )
    with pytest.raises(core.BlockedError) as blocked:
        g.preview()
    assert blocked.value.field == "nota modelo"


def test_the_form_issues_through_the_gateway_only_after_review(world):
    """The whole path a real issue takes, on screen, against the stand-in SEFIN in the test environment."""
    import asyncio

    from fiscal.tui import FiscalApp
    from tests.test_tui import SIZE, shot

    s, root, _ = world
    sefin = Sefin()

    async def script() -> None:
        app = FiscalApp(s, CFG, today=TODAY, gateway=gateway(world, sefin), data_dir=root)
        async with app.run_test(size=SIZE) as pilot:
            await pilot.press("n")
            await pilot.pause()
            # The only registered client comes selected; values and date are typed as digits only.
            await pilot.press("tab", *"920000", "tab", *"170050", "tab", *f"{TODAY:%d%m%Y}", "tab", *"Outubro")
            await pilot.press("tab", "tab", "enter")  # Review
            await pilot.pause()
            shot(app, "invoice-review-homologation")
            assert sefin.received == []  # reviewing sends nothing
            await pilot.press("tab", "enter")  # Confirm and issue
            await pilot.pause()
            shot(app, "home-after-real-issue")

    asyncio.run(script())
    assert len(sefin.received) == 1
    (issued,) = db.invoices(s, core.period_of(TODAY))
    assert (issued.number, issued.amount) == ("2", Decimal("9200.00"))
