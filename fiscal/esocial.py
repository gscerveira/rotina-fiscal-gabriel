"""eSocial web service: build, sign, batch, send and read back S-1200, S-1210 and S-1299.

Everything here follows the official documents: Manual de Orientação do Desenvolvedor v1.16
(MOD), layout S-1.3 and the communication package v1.6, whose schemas live in `schemas/esocial`.
"""

import base64
import hashlib
import re
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

import httpx
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from lxml import etree
from pydantic import Field, StringConstraints, ValidationError

from .certificate import Certificate
from .core import S1200, S1210, S1299, BlockedError, Config, Model, period_of

WHERE = "eSocial"
SCHEMAS = Path(__file__).parent / "schemas" / "esocial"
APP_VERSION = "rotina-fiscal-0.1"  # verProc
OWN_APPLICATION = "1"  # procEmi: the employer's own application
MONTHLY = "1"  # indApuracao
PAYMENT_OF_S1200 = "1"  # tpPgto
PERIODIC = 3  # batch group of S-1200, S-1210 and S-1299 (MOD 7.5.9)

EVENT_NS = "http://www.esocial.gov.br/schema/evt/{}/v_S_01_03_00"
BATCH_NS = "http://www.esocial.gov.br/schema/lote/eventos/envio/v1_1_1"
QUERY_NS = "http://www.esocial.gov.br/schema/lote/eventos/envio/consulta/retornoProcessamento/v1_0_0"
SOAP_NS = "http://schemas.xmlsoap.org/soap/envelope/"
SEND_SERVICE = "http://www.esocial.gov.br/servicos/empregador/lote/eventos/envio/v1_1_0"
QUERY_SERVICE = "http://www.esocial.gov.br/servicos/empregador/lote/eventos/envio/consulta/retornoProcessamento/v1_1_0"

LOOKUP_NS = "http://www.esocial.gov.br/schema/consulta/identificadores-eventos/{}/v1_0_0"
LOOKUP_SERVICE = "http://www.esocial.gov.br/servicos/empregador/consulta/identificadores-eventos/v1_0_0"
DOWNLOAD_NS = "http://www.esocial.gov.br/schema/download/solicitacao/id/v1_0_0"
DOWNLOAD_SERVICE = "http://www.esocial.gov.br/servicos/empregador/download/solicitacao/v1_0_0"

EventId = Annotated[str, StringConstraints(pattern=r"^ID\d{34}$")]


class Environment(StrEnum):
    PRODUCTION = "1"  # tpAmb
    RESTRICTED = "2"


SEND_URL = {
    Environment.PRODUCTION: "https://webservices.envio.esocial.gov.br/servicos/empregador/enviarloteeventos/WsEnviarLoteEventos.svc",
    Environment.RESTRICTED: "https://webservices.producaorestrita.esocial.gov.br/servicos/empregador/enviarloteeventos/WsEnviarLoteEventos.svc",
}
QUERY_URL = {
    Environment.PRODUCTION: "https://webservices.consulta.esocial.gov.br/servicos/empregador/consultarloteeventos/WsConsultarLoteEventos.svc",
    Environment.RESTRICTED: "https://webservices.producaorestrita.esocial.gov.br/servicos/empregador/consultarloteeventos/WsConsultarLoteEventos.svc",
}


_ARCHIVE = "https://webservices.{}esocial.gov.br/servicos/empregador/dwlcirurgico/{}.svc"
LOOKUP_URL = {
    Environment.PRODUCTION: _ARCHIVE.format("download.", "WsConsultarIdentificadoresEventos"),
    Environment.RESTRICTED: _ARCHIVE.format("producaorestrita.", "WsConsultarIdentificadoresEventos"),
}
DOWNLOAD_URL = {
    Environment.PRODUCTION: _ARCHIVE.format("download.", "WsSolicitarDownloadEventos"),
    Environment.RESTRICTED: _ARCHIVE.format("producaorestrita.", "WsSolicitarDownloadEventos"),
}


# --- Building events ---------------------------------------------------------


def event_id(cnpj: str, moment: datetime, sequence: int = 1) -> str:
    """ID + registration type (1 = CNPJ) + 8-digit CNPJ root padded to 14 + timestamp + sequence (MOD 8.3)."""
    return f"ID1{cnpj[:8]:0<14}{moment:%Y%m%d%H%M%S}{sequence:05d}"


def _node(parent: etree._Element, name: str, text: str | None = None) -> etree._Element:
    child = etree.SubElement(parent, f"{{{etree.QName(parent).namespace}}}{name}")
    child.text = text
    return child


def _decimal(value: Decimal) -> str:
    return f"{value:.2f}"


def _worker_cpf(cfg: Config) -> str:
    if cfg.esocial.worker_cpf is None:
        raise BlockedError("CPF do sócio", "configurado em config.toml (esocial.worker_cpf)", "ausente", WHERE)
    return cfg.esocial.worker_cpf


def _root(kind: str, identifier: str) -> tuple[etree._Element, etree._Element]:
    namespace = EVENT_NS.format(kind)
    root = etree.Element(f"{{{namespace}}}eSocial", nsmap={None: namespace})
    event = _node(root, kind)
    event.set("Id", identifier)
    return root, event


def _employer(event: etree._Element, cfg: Config) -> None:
    employer = _node(event, "ideEmpregador")
    _node(employer, "tpInsc", "1")
    _node(employer, "nrInsc", cfg.cnpj[:8])


def build(payload: S1200 | S1210 | S1299, cfg: Config, environment: Environment, identifier: str) -> etree._Element:
    """The unsigned event for a payload, in the element order of the S-1.3 schema."""
    es = cfg.esocial
    match payload:
        case S1200():
            root, event = _root("evtRemun", identifier)
            header = _node(event, "ideEvento")
            _node(header, "indRetif", "1")
            _node(header, "indApuracao", MONTHLY)
            _node(header, "perApur", payload.period)
            _node(header, "tpAmb", environment.value)
            _node(header, "procEmi", OWN_APPLICATION)
            _node(header, "verProc", APP_VERSION)
            _employer(event, cfg)
            _node(_node(event, "ideTrabalhador"), "cpfTrab", _worker_cpf(cfg))
            statement = _node(event, "dmDev")
            _node(statement, "ideDmDev", es.payslip_id)
            _node(statement, "codCateg", payload.worker_category)
            place = _node(_node(statement, "infoPerApur"), "ideEstabLot")
            _node(place, "tpInsc", "1")
            _node(place, "nrInsc", cfg.cnpj)
            _node(place, "codLotacao", payload.tax_unit)
            pay = _node(place, "remunPerApur")
            if es.worker_registration is not None:
                _node(pay, "matricula", es.worker_registration)
            if es.simples_indicator is not None:
                _node(pay, "indSimples", es.simples_indicator)
            for rubric in payload.rubrics:
                item = _node(pay, "itensRemun")
                _node(item, "codRubr", rubric.code)
                _node(item, "ideTabRubr", rubric.table)
                _node(item, "qtdRubr", _decimal(rubric.quantity))
                if rubric.factor is not None:
                    _node(item, "fatorRubr", _decimal(rubric.factor))
                _node(item, "vrRubr", _decimal(rubric.amount))
                _node(item, "indApurIR", rubric.income_tax_mode[:1])
        case S1210():
            root, event = _root("evtPgtos", identifier)
            header = _node(event, "ideEvento")
            _node(header, "indRetif", "1")
            # An S-1210 belongs to the month the money was paid in, not to the month it refers to.
            _node(header, "perApur", period_of(payload.paid_on))
            _node(header, "tpAmb", environment.value)
            _node(header, "procEmi", OWN_APPLICATION)
            _node(header, "verProc", APP_VERSION)
            _employer(event, cfg)
            beneficiary = _node(event, "ideBenef")
            _node(beneficiary, "cpfBenef", _worker_cpf(cfg))
            payment = _node(beneficiary, "infoPgto")
            _node(payment, "dtPgto", payload.paid_on.isoformat())
            _node(payment, "tpPgto", PAYMENT_OF_S1200)
            _node(payment, "perRef", payload.period)
            _node(payment, "ideDmDev", payload.payslip_id)
            _node(payment, "vrLiq", _decimal(payload.net))
        case S1299():
            root, event = _root("evtFechaEvPer", identifier)
            header = _node(event, "ideEvento")
            _node(header, "indApuracao", MONTHLY)
            _node(header, "perApur", payload.period)
            _node(header, "tpAmb", environment.value)
            _node(header, "procEmi", OWN_APPLICATION)
            _node(header, "verProc", APP_VERSION)
            _employer(event, cfg)
            closing = _node(event, "infoFech")
            for name, value in (
                ("evtRemun", payload.has_remuneration),
                ("evtPgtos", payload.has_payment),
                ("evtComProd", payload.has_rural_sales),
                ("evtContratAvNP", payload.has_dock_workers),
                ("evtInfoComplPer", payload.has_payroll_relief),
            ):
                _node(closing, name, "S" if value else "N")
            if payload.immediate_dctfweb:
                _node(closing, "transDCTFWeb", "S")  # optional for a Simples company; the user's explicit choice
            _node(closing, "naoValid", "N")  # N: run the closing validations
    return root


SCHEMA_OF = {"evtRemun": "evtRemun.xsd", "evtPgtos": "evtPgtos.xsd", "evtFechaEvPer": "evtFechaEvPer.xsd"}
LOOKUP_SCHEMA = "ConsultaIdentificadoresEventos{}-v1_0_0.xsd"
DOWNLOAD_SCHEMA = "SolicitacaoDownloadEventosPorId-v1_0_0.xsd"


def _parse(xml: bytes) -> etree._Element:
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False)
    try:
        return etree.fromstring(xml, parser)
    except etree.XMLSyntaxError:
        raise BlockedError("XML do eSocial", "bem formado", "inválido", WHERE) from None


def _check(document: etree._Element, schema_file: str, what: str) -> None:
    schema = etree.XMLSchema(etree.parse(SCHEMAS / schema_file))
    if not schema.validate(document):
        error = schema.error_log[0]
        raise BlockedError(f"esquema oficial ({what})", "válido", f"linha {error.line}: {error.message}", WHERE)


C14N = "http://www.w3.org/TR/2001/REC-xml-c14n-20010315"
DS = "http://www.w3.org/2000/09/xmldsig#"
# {xmlns} is filled only to canonicalize SignedInfo on its own; inside Signature it inherits the namespace.
SIGNED_INFO = (
    '<SignedInfo{xmlns}><CanonicalizationMethod Algorithm="' + C14N + '"/>'
    '<SignatureMethod Algorithm="http://www.w3.org/2001/04/xmldsig-more#rsa-sha256"/><Reference URI=""><Transforms>'
    '<Transform Algorithm="' + DS + 'enveloped-signature"/><Transform Algorithm="' + C14N + '"/></Transforms>'
    '<DigestMethod Algorithm="http://www.w3.org/2001/04/xmlenc#sha256"/><DigestValue>{digest}</DigestValue>'
    "</Reference></SignedInfo>"
)


def sign(event: etree._Element, certificate: Certificate, schema: str | None = None) -> bytes:
    """Enveloped XML-DSig over the whole event, exactly as MOD 6.7 and 8.4 specify.

    RSA-SHA256 over SHA-256, inclusive C14N 1.0, Reference URI="", and an unprefixed Signature as
    the last child of the root. Written out by hand: lxml canonicalizes a subtree of a larger
    document incorrectly in inclusive mode (it adds xmlns="" to descendants), and signxml, built on
    it, signs something no other implementation can verify. So only whole documents are
    canonicalized here, and a test verifies the result with xmlsec1.

    The returned bytes are final: they go into the batch verbatim, since any re-serialization
    after signing invalidates the digest.
    """
    digest = base64.b64encode(hashlib.sha256(etree.tostring(event, method="c14n")).digest()).decode()
    standalone = etree.fromstring(SIGNED_INFO.format(xmlns=f' xmlns="{DS}"', digest=digest))
    value = certificate.key.sign(etree.tostring(standalone, method="c14n"), padding.PKCS1v15(), hashes.SHA256())
    event.append(
        etree.fromstring(
            f'<Signature xmlns="{DS}">{SIGNED_INFO.format(xmlns="", digest=digest)}'
            f"<SignatureValue>{base64.b64encode(value).decode()}</SignatureValue><KeyInfo><X509Data>"
            f"<X509Certificate>{base64.b64encode(certificate.cert_der()).decode()}</X509Certificate>"
            "</X509Data></KeyInfo></Signature>"
        )
    )
    xml = etree.tostring(event)  # compact, no XML declaration: a batch may carry only its own (MOD 6.2)
    _check(_parse(xml), schema or SCHEMA_OF[etree.QName(event[0]).localname], "evento")
    return xml


def batch(signed_events: list[bytes], cfg: Config) -> bytes:
    """One batch of periodic events. The batch itself is not signed; each event is (MOD 7.5)."""
    items = []
    for xml in signed_events:
        match = re.search(rb'Id="(ID\d{34})"', xml)
        if match is None:
            raise BlockedError("evento do lote", "com Id", "sem Id", WHERE)
        items.append(b'<evento Id="' + match[1] + b'">' + xml + b"</evento>")
    document = (
        (
            f'<eSocial xmlns="{BATCH_NS}"><envioLoteEventos grupo="{PERIODIC}">'
            f"<ideEmpregador><tpInsc>1</tpInsc><nrInsc>{cfg.cnpj[:8]}</nrInsc></ideEmpregador>"
            f"<ideTransmissor><tpInsc>1</tpInsc><nrInsc>{cfg.cnpj}</nrInsc></ideTransmissor><eventos>"
        ).encode()
        + b"".join(items)
        + b"</eventos></envioLoteEventos></eSocial>"
    )
    _check(_parse(document), "EnvioLoteEventos-v1_1_1.xsd", "lote")
    return document


# --- What eSocial answers ----------------------------------------------------


class Occurrence(Model):
    code: str
    description: str
    is_error: bool  # tipo 1 = error; 2 = warning; 3 = history
    location: str | None = None

    def __str__(self) -> str:
        return f"{self.code} {self.description}" + (f" ({self.location})" if self.location else "")


class Receipt(Model):
    """The answer to a batch that was sent: accepted for processing, or refused outright."""

    status: int
    description: str
    protocol: str | None = None
    occurrences: tuple[Occurrence, ...] = ()


class EventResult(Model):
    id: EventId
    status: int
    description: str
    receipt_number: str | None = None
    occurrences: tuple[Occurrence, ...] = ()
    totals: dict[str, bytes] = Field(default_factory=dict)  # totalizer events by type (S5001, S5011...)

    @property
    def accepted(self) -> bool:
        return self.status in (201, 202) and self.receipt_number is not None


class BatchResult(Model):
    status: int
    description: str
    wait_seconds: int = 0  # how long eSocial asks us to wait before asking again
    occurrences: tuple[Occurrence, ...] = ()
    events: tuple[EventResult, ...] = ()

    @property
    def state(self) -> Literal["waiting", "processed", "failed"]:
        if self.status == 101:
            return "waiting"
        return "processed" if self.status in (201, 202) else "failed"


def _local(element: etree._Element, *path: str) -> etree._Element | None:
    """Find by local names, ignoring namespaces: every eSocial answer uses a different one."""
    found = element.xpath("/".join(f"*[local-name()='{name}']" for name in path))
    return found[0] if isinstance(found, list) and found and isinstance(found[0], etree._Element) else None


def _text(element: etree._Element | None, *path: str) -> str | None:
    found = _local(element, *path) if element is not None else None
    return found.text if found is not None else None


def _occurrences(holder: etree._Element | None) -> tuple[Occurrence, ...]:
    if holder is None:
        return ()
    found = holder.xpath("*[local-name()='ocorrencias']/*[local-name()='ocorrencia']")
    return tuple(
        Occurrence(
            code=_text(o, "codigo") or "?",
            description=(_text(o, "descricao") or "").strip(),
            is_error=_text(o, "tipo") == "1",
            location=_text(o, "localizacao"),
        )
        for o in (found if isinstance(found, list) else [])
        if isinstance(o, etree._Element)
    )


class EventRef(Model):
    """Where to find an event already on file at eSocial."""

    id: str
    receipt_number: str


def _archive_status(status: etree._Element | None) -> None:
    code = _text(status, "cdResposta")
    if code != "201":
        raise BlockedError(
            "consulta ao eSocial", "201", f"{code} {(_text(status, 'descResposta') or '').strip()}", WHERE
        )


def _answer(soap: bytes, name: str) -> etree._Element:
    """The eSocial document inside a SOAP response, or a block when the response is not one."""
    found = _parse(soap).xpath(f"//*[local-name()='{name}']")
    if not (isinstance(found, list) and found and isinstance(found[0], etree._Element)):
        raise BlockedError("resposta do eSocial", name, "ausente", WHERE)
    return found[0]


def read_receipt(soap: bytes) -> Receipt:
    answer = _answer(soap, "retornoEnvioLoteEventos")
    status = _local(answer, "status")
    try:
        return Receipt(
            status=int(_text(status, "cdResposta") or 0),
            description=_text(status, "descResposta") or "",
            protocol=_text(answer, "dadosRecepcaoLote", "protocoloEnvio"),
            occurrences=_occurrences(status),
        )
    except ValidationError as invalid:
        raise BlockedError.from_validation(invalid, WHERE) from invalid


def read_result(soap: bytes) -> BatchResult:
    answer = _answer(soap, "retornoProcessamentoLoteEventos")
    status = _local(answer, "status")
    events = []
    found = answer.xpath("*[local-name()='retornoEventos']/*[local-name()='evento']")
    for event in found if isinstance(found, list) else []:
        if not isinstance(event, etree._Element):
            continue
        outcome = _local(event, "retornoEvento", "eSocial", "retornoEvento")
        processing = _local(outcome, "processamento") if outcome is not None else None
        totals = {
            str(total.get("tipo")): etree.tostring(total[0])
            for total in event.xpath("*[local-name()='tot']")
            if isinstance(total, etree._Element) and len(total)
        }
        events.append(
            EventResult(
                id=event.get("Id", ""),
                status=int(_text(processing, "cdResposta") or 0),
                description=_text(processing, "descResposta") or "",
                receipt_number=_text(outcome, "recibo", "nrRecibo"),
                occurrences=_occurrences(processing),
                totals=totals,
            )
        )
    return BatchResult(
        status=int(_text(status, "cdResposta") or 0),
        description=_text(status, "descResposta") or "",
        wait_seconds=int(_text(status, "tempoEstimadoConclusao") or 0),
        occurrences=_occurrences(status),
        events=tuple(events),
    )


def withheld_inss(s1200_result: EventResult) -> Decimal | None:
    """What eSocial itself computed as the INSS withheld from the worker (S-5001, infoCpCalc/vrDescSeg)."""
    total = s1200_result.totals.get("S5001")
    value = _text(_parse(total), "evtBasesTrab", "infoCpCalc", "vrDescSeg") if total else None
    return Decimal(value) if value else None


# --- Talking to the web service (SOAP 1.1 over mutual TLS) -------------------


def _envelope(service: str, operation: str, parameter: str, document: bytes) -> bytes:
    return (
        (
            f'<soapenv:Envelope xmlns:soapenv="{SOAP_NS}" xmlns:v1="{service}"><soapenv:Header/><soapenv:Body>'
            f"<v1:{operation}><v1:{parameter}>"
        ).encode()
        + document
        + f"</v1:{parameter}></v1:{operation}></soapenv:Body></soapenv:Envelope>".encode()
    )


ARCHIVE_CLOSED_UNTIL_DAY = 7  # MOD 7.7.7 and 7.8.7: lookups and downloads are refused on days 1 to 7
ARCHIVE_CALLS_PER_DAY = 10  # same sections: lookups and downloads share this daily allowance


def archive_reopens_on(today: date) -> date | None:
    """When the archive of sent events opens again, or None if it is open today. Sending is never closed."""
    return today.replace(day=ARCHIVE_CLOSED_UNTIL_DAY + 1) if today.day <= ARCHIVE_CLOSED_UNTIL_DAY else None


class EsocialClient:
    def __init__(self, http: httpx.Client, environment: Environment, today: Callable[[], date] = date.today) -> None:
        self.http, self.environment, self.today = http, environment, today
        self.archive_calls = 0  # made by this client; the caller keeps the daily total

    def _archive(self) -> None:
        """Refuse an archive call the government would refuse anyway, before spending one of the day's ten."""
        reopens = archive_reopens_on(self.today())
        if reopens is not None:
            raise BlockedError(
                "consulta ao eSocial", f"a partir de {reopens:%d/%m/%Y}", "fechada nos dias 1 a 7 de cada mês", WHERE
            )
        self.archive_calls += 1

    def _call(self, url: str, service: str, port: str, operation: str, parameter: str, document: bytes) -> bytes:
        response = self.http.post(
            url,
            content=_envelope(service, operation, parameter, document),
            headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": f'"{service}/{port}/{operation}"'},
        )
        if response.status_code != 200:
            raise BlockedError("resposta do eSocial", "HTTP 200", f"HTTP {response.status_code}", WHERE)
        return response.content

    # --- the archive: events this employer already sent, by any means (MOD 7.7 and 7.8) ---
    # Closed on days 1 to 7 of each month and limited to 10 calls a day across lookups and downloads.

    def _lookup(self, kind: str, filters: dict[str, str], cfg: Config, certificate: Certificate) -> list[EventRef]:
        namespace = LOOKUP_NS.format(kind.lower())
        root = etree.Element(f"{{{namespace}}}eSocial", nsmap={None: namespace})
        query = _node(root, "consultaIdentificadoresEvts")
        _employer(query, cfg)
        criteria = _node(query, f"consultaEvts{kind}")
        for name, value in filters.items():
            _node(criteria, name, value)
        document = sign(root, certificate, LOOKUP_SCHEMA.format(kind))
        self._archive()
        operation = f"ConsultarIdentificadoresEventos{kind}"
        answer = _answer(
            self._call(
                LOOKUP_URL[self.environment],
                LOOKUP_SERVICE,
                "ServicoConsultarIdentificadoresEventos",
                operation,
                f"consultaEventos{kind}",
                document,
            ),
            "retornoConsultaIdentificadoresEvts",
        )
        _archive_status(_local(answer, "status"))
        found = answer.xpath(".//*[local-name()='identificadorEvt']")
        return [
            EventRef(id=_text(item, "id") or "", receipt_number=_text(item, "nrRec") or "")
            for item in (found if isinstance(found, list) else [])
            if isinstance(item, etree._Element)
        ]

    def worker_events(
        self, cpf: str, received_from: datetime, received_to: datetime, cfg: Config, certificate: Certificate
    ) -> list[EventRef]:
        """Events about one worker (S-1200, S-1210, S-2300...) received in a window of at most 31 days."""
        window = {
            "cpfTrab": cpf,
            "dtIni": f"{received_from:%Y-%m-%dT%H:%M:%S}",
            "dtFim": f"{received_to:%Y-%m-%dT%H:%M:%S}",
        }
        return self._lookup("Trabalhador", window, cfg, certificate)

    def employer_events(self, event_type: str, period: str, cfg: Config, certificate: Certificate) -> list[EventRef]:
        """Employer-level periodic events of one period, such as S-1299."""
        return self._lookup("Empregador", {"tpEvt": event_type, "perApur": period}, cfg, certificate)

    def table_events(self, event_type: str, cfg: Config, certificate: Certificate) -> list[EventRef]:
        """Table events, such as S-1000 (employer data) or S-1010 (rubrics)."""
        return self._lookup("Tabela", {"tpEvt": event_type}, cfg, certificate)

    def download(self, ids: list[str], cfg: Config, certificate: Certificate) -> dict[str, bytes]:
        """The XML of up to 50 events, by Id."""
        root = etree.Element(f"{{{DOWNLOAD_NS}}}eSocial", nsmap={None: DOWNLOAD_NS})
        request = _node(root, "download")
        _employer(request, cfg)
        wanted = _node(request, "solicDownloadEvtsPorId")
        for identifier in ids:
            _node(wanted, "id", identifier)
        document = sign(root, certificate, DOWNLOAD_SCHEMA)
        self._archive()
        answer = _answer(
            self._call(
                DOWNLOAD_URL[self.environment],
                DOWNLOAD_SERVICE,
                "ServicoSolicitarDownloadEventos",
                "SolicitarDownloadEventosPorId",
                "solicitacao",
                document,
            ),
            "download",
        )
        _archive_status(_local(answer, "status"))
        events: dict[str, bytes] = {}
        found = answer.xpath(".//*[local-name()='arquivo']/*[local-name()='evt']")
        for holder in found if isinstance(found, list) else []:
            if isinstance(holder, etree._Element) and len(holder):
                events[holder.get("Id", "")] = etree.tostring(holder[0])
        return events

    def send(self, batch_xml: bytes) -> Receipt:
        """Hand a batch over. Never retried here: a lost answer is recovered by sending the identical batch again."""
        url = SEND_URL[self.environment]
        return read_receipt(
            self._call(url, SEND_SERVICE, "ServicoEnviarLoteEventos", "EnviarLoteEventos", "loteEventos", batch_xml)
        )

    def result(self, protocol: str) -> BatchResult:
        query = (
            f'<eSocial xmlns="{QUERY_NS}"><consultaLoteEventos><protocoloEnvio>{protocol}</protocoloEnvio>'
            "</consultaLoteEventos></eSocial>"
        ).encode()
        _check(_parse(query), "ConsultaLoteEventos-v1_0_0.xsd", "consulta")
        url = QUERY_URL[self.environment]
        return read_result(
            self._call(url, QUERY_SERVICE, "ServicoConsultarLoteEventos", "ConsultarLoteEventos", "consulta", query)
        )
