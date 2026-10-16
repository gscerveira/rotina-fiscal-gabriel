import base64
import hashlib
import shutil
import subprocess
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from lxml import etree

from fiscal import certificate, core, esocial
from tests.test_certificate import PASSWORD, make_pfx

BASE = core.load_config(Path(__file__).parent.parent / "config.toml")
CFG = BASE.model_copy(
    update={"esocial": BASE.esocial.model_copy(update={"worker_cpf": "12345678909", "worker_registration": "SOCIO1"})}
)
PERIOD = "2026-09"
TODAY = date(2026, 10, 7)
MOMENT = datetime(2026, 10, 7, 10, 30, 0)
DS = "{http://www.w3.org/2000/09/xmldsig#}"
RESTRICTED = esocial.Environment.RESTRICTED
SCHEMAS = esocial.SCHEMAS


def payload(step: str, paid_on: date | None = None) -> core.Payload:
    return core.build(core.STEP_BY_ID[step], CFG, PERIOD, None, paid_on)


def text(root: etree._Element, name: str) -> list[str | None]:
    return [e.text for e in root.iter() if etree.QName(e).localname == name]


@pytest.fixture
def loaded(tmp_path: Path) -> certificate.Certificate:
    return certificate.load(make_pfx(tmp_path, f"ACME LTDA:{CFG.cnpj}"), PASSWORD, CFG.cnpj, TODAY)


def test_event_id_follows_the_manual():
    identifier = esocial.event_id(CFG.cnpj, MOMENT, 2)
    assert identifier == f"ID1{CFG.cnpj[:8]}000000" + "20261007103000" + "00002"
    assert len(identifier) == 36


def test_s1200_carries_both_rubrics_and_the_worker_as_esocial_knows_them():
    p = payload("s1200")
    assert isinstance(p, core.S1200)
    root = esocial.build(p, CFG, RESTRICTED, esocial.event_id(CFG.cnpj, MOMENT))
    assert text(root, "perApur") == ["2026-09"]
    assert text(root, "tpAmb") == ["2"]
    assert text(root, "nrInsc") == [CFG.cnpj[:8], CFG.cnpj]  # employer by CNPJ root, establishment in full
    assert text(root, "cpfTrab") == ["12345678909"]
    assert (text(root, "codCateg"), text(root, "codLotacao"), text(root, "matricula")) == (["723"], ["1"], ["SOCIO1"])
    assert text(root, "codRubr") == ["85", "1950"]
    assert text(root, "qtdRubr") == ["220.00", "11.00"]
    assert text(root, "vrRubr") == ["1621.00", "178.31"]
    assert text(root, "fatorRubr") == []  # 11 goes in the quantity; the factor stays out
    assert text(root, "indSimples") == []  # only for classTrib 03


def test_optional_worker_fields_follow_the_configuration():
    p = payload("s1200")
    assert isinstance(p, core.S1200)
    es = CFG.esocial.model_copy(update={"worker_registration": None, "simples_indicator": "2"})
    root = esocial.build(p, CFG.model_copy(update={"esocial": es}), RESTRICTED, esocial.event_id(CFG.cnpj, MOMENT))
    assert (text(root, "matricula"), text(root, "indSimples")) == ([], ["2"])
    unset = BASE.model_copy(update={"esocial": BASE.esocial.model_copy(update={"worker_cpf": None})})
    with pytest.raises(core.BlockedError) as blocked:
        esocial.build(p, unset, RESTRICTED, esocial.event_id(BASE.cnpj, MOMENT))
    assert blocked.value.field == "CPF do sócio"


def test_s1210_belongs_to_the_month_of_payment_and_refers_to_the_month_earned():
    p = payload("s1210", date(2026, 10, 5))
    assert isinstance(p, core.S1210)
    root = esocial.build(p, CFG, RESTRICTED, esocial.event_id(CFG.cnpj, MOMENT))
    assert (text(root, "perApur"), text(root, "perRef"), text(root, "dtPgto")) == (
        ["2026-10"],
        ["2026-09"],
        ["2026-10-05"],
    )
    assert (text(root, "tpPgto"), text(root, "ideDmDev"), text(root, "vrLiq")) == (["1"], ["001"], ["1442.69"])


def test_s1299_sends_the_dctfweb_request_only_when_configured():
    p = payload("s1299")
    assert isinstance(p, core.S1299)
    root = esocial.build(p, CFG, RESTRICTED, esocial.event_id(CFG.cnpj, MOMENT))
    flags = {
        name: text(root, name) for name in ("evtRemun", "evtPgtos", "evtComProd", "evtContratAvNP", "evtInfoComplPer")
    }
    assert flags == {
        "evtRemun": ["S"],
        "evtPgtos": ["S"],
        "evtComProd": ["N"],
        "evtContratAvNP": ["N"],
        "evtInfoComplPer": ["N"],
    }
    assert (text(root, "transDCTFWeb"), text(root, "naoValid")) == ([], ["N"])
    asked = esocial.build(
        p.model_copy(update={"immediate_dctfweb": True}), CFG, RESTRICTED, esocial.event_id(CFG.cnpj, MOMENT)
    )
    assert text(asked, "transDCTFWeb") == ["S"]


@pytest.mark.parametrize(("step", "paid_on"), [("s1200", None), ("s1210", date(2026, 9, 30)), ("s1299", None)])
def test_signed_events_pass_the_official_schema_and_verify(
    loaded: certificate.Certificate, tmp_path: Path, step: str, paid_on: date | None
):
    p = payload(step, paid_on)
    assert isinstance(p, core.S1200 | core.S1210 | core.S1299)
    signed = esocial.sign(esocial.build(p, CFG, RESTRICTED, esocial.event_id(CFG.cnpj, MOMENT)), loaded)  # validates
    assert not signed.startswith(b"<?xml")
    root = etree.fromstring(signed)
    event, signature = root
    assert signature.tag == f"{DS}Signature"
    assert signature.prefix is None
    assert signature.find(f".//{DS}Reference").get("URI") == ""  # ty: ignore[unresolved-attribute]
    algorithms = [e.get("Algorithm") for e in signature.iter() if e.get("Algorithm")]
    assert algorithms == [
        "http://www.w3.org/TR/2001/REC-xml-c14n-20010315",
        "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256",
        "http://www.w3.org/2000/09/xmldsig#enveloped-signature",
        "http://www.w3.org/TR/2001/REC-xml-c14n-20010315",
        "http://www.w3.org/2001/04/xmlenc#sha256",
    ]
    # Independent check: the digest covers the whole document without the Signature element.
    stripped = etree.fromstring(signed)
    stripped.remove(stripped[1])
    digest = hashlib.sha256(etree.tostring(stripped, method="c14n")).digest()
    assert base64.b64encode(digest).decode() == signature.findtext(f".//{DS}DigestValue")
    assert event.get("Id") == esocial.event_id(CFG.cnpj, MOMENT)
    # The signature itself is checked by xmlsec1, the reference XML-DSig implementation. (lxml cannot
    # canonicalize SignedInfo in place correctly, so checking it with lxml would only repeat the mistake.)
    if shutil.which("xmlsec1"):
        (tmp_path / "event.xml").write_bytes(signed)
        (tmp_path / "cert.pem").write_bytes(loaded.cert.public_bytes(serialization.Encoding.PEM))
        check = subprocess.run(  # noqa: S603 - fixed arguments, files written by this test
            ["xmlsec1", "--verify", "--trusted-pem", str(tmp_path / "cert.pem"), str(tmp_path / "event.xml")],  # noqa: S607
            capture_output=True,
            text=True,
            check=False,
        )
        assert check.returncode == 0, check.stderr


def signed_pair(loaded: certificate.Certificate) -> list[bytes]:
    events = []
    for sequence, (step, paid_on) in enumerate([("s1200", None), ("s1210", date(2026, 9, 30))], start=1):
        p = payload(step, paid_on)
        assert isinstance(p, core.S1200 | core.S1210)
        events.append(
            esocial.sign(esocial.build(p, CFG, RESTRICTED, esocial.event_id(CFG.cnpj, MOMENT, sequence)), loaded)
        )
    return events


def test_batch_embeds_the_signed_bytes_untouched(loaded: certificate.Certificate):
    events = signed_pair(loaded)
    document = esocial.batch(events, CFG)  # validates against the official batch schema
    assert all(xml in document for xml in events)
    root = etree.fromstring(document)
    assert root[0].get("grupo") == "3"
    assert text(root[0], "nrInsc")[:2] == [CFG.cnpj[:8], CFG.cnpj]  # employer root, transmitter in full
    assert [e.get("Id") for e in root.iter() if etree.QName(e).localname == "evento"] == [
        esocial.event_id(CFG.cnpj, MOMENT, 1),
        esocial.event_id(CFG.cnpj, MOMENT, 2),
    ]


# --- answers, shaped after the official communication schemas -------------------

RECEIPT_NS = "http://www.esocial.gov.br/schema/lote/eventos/envio/retornoEnvio/v1_1_0"
RESULT_NS = "http://www.esocial.gov.br/schema/lote/eventos/envio/retornoProcessamento/v1_3_0"
EVENT_RESULT_NS = "http://www.esocial.gov.br/schema/evt/retornoEvento/v1_3_0"
EMPLOYER = f"<ideEmpregador><tpInsc>1</tpInsc><nrInsc>{CFG.cnpj[:8]}</nrInsc></ideEmpregador>"
TRANSMITTER = f"<ideTransmissor><tpInsc>1</tpInsc><nrInsc>{CFG.cnpj}</nrInsc></ideTransmissor>"
PROTOCOL = "1.2.202610.0000000000000000001"


def soap(operation: str, document: str) -> bytes:
    return (
        '<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
        f'<{operation}Response xmlns="urn:x"><{operation}Result>{document}</{operation}Result></{operation}Response>'
        "</s:Body></s:Envelope>"
    ).encode()


def receipt_xml(status: int, occurrences: str = "", protocol: str | None = PROTOCOL) -> str:
    reception = (
        f"<dadosRecepcaoLote><dhRecepcao>2026-10-07T10:31:00</dhRecepcao>"
        f"<versaoAplicativoRecepcao>1.0</versaoAplicativoRecepcao><protocoloEnvio>{protocol}</protocoloEnvio></dadosRecepcaoLote>"
        if protocol
        else ""
    )
    return (
        f'<eSocial xmlns="{RECEIPT_NS}"><retornoEnvioLoteEventos>{EMPLOYER}{TRANSMITTER}'
        f"<status><cdResposta>{status}</cdResposta><descResposta>x</descResposta>{occurrences}</status>{reception}"
        "</retornoEnvioLoteEventos></eSocial>"
    )


def result_xml(status: int, events: str = "", wait: int | None = None) -> str:
    estimate = f"<tempoEstimadoConclusao>{wait}</tempoEstimadoConclusao>" if wait is not None else ""
    return (
        f'<eSocial xmlns="{RESULT_NS}"><retornoProcessamentoLoteEventos>{EMPLOYER}{TRANSMITTER}'
        f"<status><cdResposta>{status}</cdResposta><descResposta>x</descResposta>{estimate}</status>"
        f"{f'<retornoEventos>{events}</retornoEventos>' if events else ''}</retornoProcessamentoLoteEventos></eSocial>"
    )


def event_result(identifier: str, status: int, receipt: str | None, occurrences: str = "", totals: str = "") -> str:
    proof = f"<recibo><nrRecibo>{receipt}</nrRecibo><hash>abc</hash></recibo>" if receipt else ""
    return (
        f'<evento Id="{identifier}"><retornoEvento><eSocial xmlns="{EVENT_RESULT_NS}"><retornoEvento Id="{identifier}">'
        f"{EMPLOYER}<recepcao><tpAmb>2</tpAmb><dhRecepcao>2026-10-07T10:31:00</dhRecepcao>"
        f"<versaoAppRecepcao>1</versaoAppRecepcao><protocoloEnvioLote>{PROTOCOL}</protocoloEnvioLote></recepcao>"
        f"<processamento><cdResposta>{status}</cdResposta><descResposta>x</descResposta>"
        f"<versaoAppProcessamento>1</versaoAppProcessamento><dhProcessamento>2026-10-07T10:31:05</dhProcessamento>"
        f"{occurrences}</processamento>{proof}</retornoEvento></eSocial></retornoEvento>{totals}</evento>"
    )


S5001 = (
    '<tot tipo="S5001"><eSocial xmlns="http://www.esocial.gov.br/schema/evt/evtBasesTrab/v_S_01_03_00">'
    "<evtBasesTrab><infoCpCalc><tpCR>109901</tpCR><vrCpSeg>178.31</vrCpSeg><vrDescSeg>178.31</vrDescSeg></infoCpCalc>"
    "</evtBasesTrab></eSocial></tot>"
)


def test_my_sample_answers_follow_the_official_schemas():
    for xml, schema in (
        (receipt_xml(201), "RetornoEnvioLoteEventos-v1_1_0.xsd"),
        (result_xml(101, wait=8), "RetornoProcessamentoLote-v1_3_0.xsd"),
    ):
        etree.XMLSchema(etree.parse(SCHEMAS / schema)).assertValid(etree.fromstring(xml))


def test_client_sends_soap_1_1_and_reads_the_protocol(loaded: certificate.Certificate):
    seen: list[httpx.Request] = []

    def server(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=soap("EnviarLoteEventos", receipt_xml(201)))

    client = esocial.EsocialClient(httpx.Client(transport=httpx.MockTransport(server)), RESTRICTED)
    document = esocial.batch(signed_pair(loaded), CFG)
    receipt = client.send(document)
    assert (receipt.status, receipt.protocol) == (201, PROTOCOL)
    (request,) = seen
    assert str(request.url) == esocial.SEND_URL[RESTRICTED]
    assert "producaorestrita" in request.url.host
    assert request.headers["Content-Type"] == "text/xml; charset=utf-8"
    assert request.headers["SOAPAction"] == f'"{esocial.SEND_SERVICE}/ServicoEnviarLoteEventos/EnviarLoteEventos"'
    assert document in request.content  # the batch travels verbatim inside the envelope
    etree.fromstring(request.content)


def test_a_refused_batch_reports_why_and_has_no_protocol():
    why = (
        "<ocorrencias><ocorrencia><codigo>607</codigo><descricao>Transmissor difere do certificado</descricao>"
        "<tipo>1</tipo></ocorrencia></ocorrencias>"
    )
    receipt = esocial.read_receipt(soap("EnviarLoteEventos", receipt_xml(401, why, protocol=None)))
    assert (receipt.status, receipt.protocol) == (401, None)
    assert str(receipt.occurrences[0]) == "607 Transmissor difere do certificado"


def test_result_waits_then_reports_receipts_totals_and_rejections():
    one, two = esocial.event_id(CFG.cnpj, MOMENT, 1), esocial.event_id(CFG.cnpj, MOMENT, 2)
    waiting = esocial.read_result(soap("ConsultarLoteEventos", result_xml(101, wait=8)))
    assert (waiting.state, waiting.wait_seconds, waiting.events) == ("waiting", 8, ())

    refusal = (
        "<ocorrencias><ocorrencia><tipo>1</tipo><codigo>MS0106</codigo><descricao>Evento duplicado</descricao>"
        "<localizacao>/eSocial/evtPgtos</localizacao></ocorrencia></ocorrencias>"
    )
    done = esocial.read_result(
        soap(
            "ConsultarLoteEventos",
            result_xml(
                201,
                event_result(one, 201, "1.2.0000000000000000001", totals=S5001) + event_result(two, 401, None, refusal),
            ),
        )
    )
    assert done.state == "processed"
    accepted, refused = done.events
    assert (accepted.accepted, accepted.receipt_number) == (True, "1.2.0000000000000000001")
    assert esocial.withheld_inss(accepted) == Decimal("178.31")
    assert (refused.accepted, refused.status) == (False, 401)
    assert str(refused.occurrences[0]) == "MS0106 Evento duplicado (/eSocial/evtPgtos)"


def test_client_queries_by_protocol_and_blocks_on_http_errors():
    def server(request: httpx.Request) -> httpx.Response:
        assert PROTOCOL.encode() in request.content
        assert request.headers["SOAPAction"].endswith('/ServicoConsultarLoteEventos/ConsultarLoteEventos"')
        return httpx.Response(200, content=soap("ConsultarLoteEventos", result_xml(101, wait=5)))

    client = esocial.EsocialClient(httpx.Client(transport=httpx.MockTransport(server)), RESTRICTED)
    assert client.result(PROTOCOL).state == "waiting"

    down = esocial.EsocialClient(httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(500))), RESTRICTED)
    with pytest.raises(core.BlockedError) as blocked:
        down.result(PROTOCOL)
    assert blocked.value.found == "HTTP 500"


# --- the archive of events already sent ----------------------------------------

OPEN_DAY = date(2026, 10, 8)
LOOKUP_RESULT_NS = "http://www.esocial.gov.br/schema/consulta/identificadores-eventos/retorno/v1_0_0"
DOWNLOAD_RESULT_NS = "http://www.esocial.gov.br/schema/download/solicitacao/retorno/v1_0_0"


def lookup_xml(status: int, description: str = "Sucesso", refs: tuple[tuple[str, str], ...] = ()) -> str:
    listed = "".join(f"<identificadorEvt><id>{i}</id><nrRec>{r}</nrRec></identificadorEvt>" for i, r in refs)
    found = (
        f"<retornoIdentificadoresEvts><qtdeTotEvtsConsulta>{len(refs)}</qtdeTotEvtsConsulta>"
        f"<identificadoresEvts>{listed}</identificadoresEvts></retornoIdentificadoresEvts>"
        if refs
        else ""
    )
    return (
        f'<eSocial xmlns="{LOOKUP_RESULT_NS}"><retornoConsultaIdentificadoresEvts>'
        f"<status><cdResposta>{status}</cdResposta><descResposta>{description}</descResposta></status>{found}"
        "</retornoConsultaIdentificadoresEvts></eSocial>"
    )


def test_archive_lookups_are_signed_and_follow_the_official_schemas(loaded: certificate.Certificate):
    one = esocial.event_id(CFG.cnpj, MOMENT, 1)
    seen: list[httpx.Request] = []

    def server(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        operation = request.headers["SOAPAction"].strip('"').rsplit("/", 1)[1]
        return httpx.Response(200, content=soap(operation, lookup_xml(201, refs=((one, "1.1.0000000000000000009"),))))

    client = esocial.EsocialClient(
        httpx.Client(transport=httpx.MockTransport(server)), esocial.Environment.PRODUCTION, today=lambda: OPEN_DAY
    )
    start, end = datetime(2026, 9, 8), datetime(2026, 10, 8)
    by_worker = client.worker_events("12345678909", start, end, CFG, loaded)  # each query is schema-checked when signed
    by_employer = client.employer_events("S-1299", "2026-09", CFG, loaded)
    by_table = client.table_events("S-1000", CFG, loaded)
    assert by_worker == by_employer == by_table == [esocial.EventRef(id=one, receipt_number="1.1.0000000000000000009")]

    assert [str(r.url) for r in seen] == [esocial.LOOKUP_URL[esocial.Environment.PRODUCTION]] * 3
    assert [r.headers["SOAPAction"].strip('"').rsplit("/", 1)[1] for r in seen] == [
        "ConsultarIdentificadoresEventosTrabalhador",
        "ConsultarIdentificadoresEventosEmpregador",
        "ConsultarIdentificadoresEventosTabela",
    ]
    worker_query = seen[0].content
    assert b"<v1:consultaEventosTrabalhador>" in worker_query
    assert (
        b"<cpfTrab>12345678909</cpfTrab><dtIni>2026-09-08T00:00:00</dtIni><dtFim>2026-10-08T00:00:00</dtFim>"
        in worker_query
    )
    assert b"<Signature xmlns=" in worker_query
    assert b"<tpEvt>S-1299</tpEvt><perApur>2026-09</perApur>" in seen[1].content


def test_archive_reports_when_it_is_closed(loaded: certificate.Certificate):
    closed = soap("ConsultarIdentificadoresEventosTabela", lookup_xml(403, "Consulta não permitida nos dias 1 a 7"))
    client = esocial.EsocialClient(
        httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, content=closed))),
        RESTRICTED,
        today=lambda: OPEN_DAY,
    )
    with pytest.raises(core.BlockedError) as blocked:
        client.table_events("S-1000", CFG, loaded)
    assert blocked.value.found.startswith("403 ")


def test_download_returns_each_event_xml_by_id(loaded: certificate.Certificate):
    one = esocial.event_id(CFG.cnpj, MOMENT, 1)
    p = payload("s1200")
    assert isinstance(p, core.S1200)
    stored = esocial.sign(esocial.build(p, CFG, RESTRICTED, one), loaded).decode()
    answer = (
        f'<eSocial xmlns="{DOWNLOAD_RESULT_NS}"><download><status><cdResposta>201</cdResposta>'
        "<descResposta>Sucesso</descResposta></status><retornoSolicDownloadEvts><arquivos><arquivo>"
        f'<status><cdResposta>201</cdResposta><descResposta>ok</descResposta></status><evt Id="{one}">{stored}</evt>'
        "</arquivo></arquivos></retornoSolicDownloadEvts></download></eSocial>"
    )
    seen: list[httpx.Request] = []

    def server(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=soap("SolicitarDownloadEventosPorId", answer))

    client = esocial.EsocialClient(
        httpx.Client(transport=httpx.MockTransport(server)), RESTRICTED, today=lambda: OPEN_DAY
    )
    events = client.download([one], CFG, loaded)
    assert client.archive_calls == 1
    assert list(events) == [one]
    assert text(etree.fromstring(events[one]), "matricula") == ["SOCIO1"]
    assert f"<id>{one}</id>".encode() in seen[0].content
    assert seen[0].headers["SOAPAction"].endswith('/ServicoSolicitarDownloadEventos/SolicitarDownloadEventosPorId"')


def test_the_archive_is_not_even_called_on_days_1_to_7_but_sending_always_works(loaded: certificate.Certificate):
    calls: list[str] = []

    def server(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, content=soap("EnviarLoteEventos", receipt_xml(201)))

    client = esocial.EsocialClient(
        httpx.Client(transport=httpx.MockTransport(server)), RESTRICTED, today=lambda: date(2026, 10, 7)
    )
    for attempt in (
        lambda: client.table_events("S-1000", CFG, loaded),
        lambda: client.employer_events("S-1299", "2026-09", CFG, loaded),
        lambda: client.download([esocial.event_id(CFG.cnpj, MOMENT)], CFG, loaded),
    ):
        with pytest.raises(core.BlockedError) as blocked:
            attempt()
        assert blocked.value.expected == "a partir de 08/10/2026"
    assert (calls, client.archive_calls) == ([], 0)  # nothing left the machine, no daily call was spent

    assert client.send(esocial.batch(signed_pair(loaded), CFG)).status == 201  # sending has no closed days
    assert esocial.archive_reopens_on(date(2026, 10, 8)) is None
    assert esocial.archive_reopens_on(date(2026, 11, 1)) == date(2026, 11, 8)
