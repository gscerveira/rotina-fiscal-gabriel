"""NFS-e Nacional, read side: pull what was issued from the ADN and file the XML and the PDF.

Only documents this company issued count. The ADN also distributes notes issued *against* the
company (it as the client) and events such as cancellations; the first are ignored, the second
change the status of our invoices.
"""

import base64
import gzip
import re
from collections.abc import Iterable
from datetime import date
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal

import httpx
from lxml import etree
from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, StringConstraints, ValidationError, computed_field
from sqlalchemy.orm import Session

from . import db
from .certificate import Certificate
from .core import BlockedError, Config, Model, NonEmpty, Period, PositiveMoney, period_of, shift

WHERE = "NFS-e Nacional"
NAMESPACE = {"n": "http://www.sped.fazenda.gov.br/nfse"}
LAST_NSU = "adn_last_nsu"
MAX_BATCHES = 40  # the ADN publishes no ceiling; never loop forever on a misbehaving server
EXPORT_OF_SERVICE = "3"  # tribISSQN
AUTHORIZED = "100"  # cStat
CANCELLATIONS = {"e101101", "e105102"}  # cancellation, cancellation by substitution

AccessKey = Annotated[str, StringConstraints(pattern=r"^\d{50}$")]


class Environment(StrEnum):
    PRODUCTION = "PRODUCAO"
    RESTRICTED = "HOMOLOGACAO"  # "produção restrita": the government's test environment


ADN = {
    Environment.PRODUCTION: "https://adn.nfse.gov.br",
    Environment.RESTRICTED: "https://adn.producaorestrita.nfse.gov.br",
}

SEFIN = {
    Environment.PRODUCTION: "https://sefin.nfse.gov.br/SefinNacional",
    Environment.RESTRICTED: "https://sefin.producaorestrita.nfse.gov.br/SefinNacional",
}


# --- What the ADN sends -----------------------------------------------------


def _unzip(value: object) -> object:
    """Every XML travels as base64(gzip(xml))."""
    if not isinstance(value, str):
        return value
    try:
        return gzip.decompress(base64.b64decode(value, validate=True))
    except ValueError, OSError:
        raise ValueError("not base64-encoded gzip") from None


def _none_as_empty(value: object) -> object:
    return () if value is None else value


class Wire(BaseModel):
    """A government payload: read leniently (fields may be added), never mutated."""

    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)


class DfeError(Wire):
    code: str = Field(alias="Codigo")
    description: str | None = Field(default=None, alias="Descricao")


class DfeDocument(Wire):
    nsu: Annotated[int, Field(alias="NSU", gt=0)]
    key: Annotated[AccessKey, Field(alias="ChaveAcesso")]
    kind: Annotated[str, Field(alias="TipoDocumento")]
    xml: Annotated[bytes, BeforeValidator(_unzip), Field(alias="ArquivoXml")]


class DfeBatch(Wire):
    status: Annotated[
        Literal["DOCUMENTOS_LOCALIZADOS", "NENHUM_DOCUMENTO_LOCALIZADO", "REJEICAO"],
        Field(alias="StatusProcessamento"),
    ]
    environment: Annotated[Environment, Field(alias="TipoAmbiente")]
    documents: Annotated[tuple[DfeDocument, ...], BeforeValidator(_none_as_empty), Field(alias="LoteDFe")] = ()
    errors: Annotated[tuple[DfeError, ...], BeforeValidator(_none_as_empty), Field(alias="Erros")] = ()


# --- What an NFS-e says -----------------------------------------------------


class Nfse(Model):
    """The fields of an issued NFS-e this program relies on."""

    key: AccessKey
    number: NonEmpty
    issuer: NonEmpty  # CNPJ or CPF of whoever issued it
    situation: NonEmpty  # cStat
    service_date: date  # dCompet
    amount: PositiveMoney
    client: NonEmpty
    description: NonEmpty
    service_code: Annotated[str, StringConstraints(pattern=r"^\d{6}$")]
    iss_taxation: NonEmpty  # tribISSQN

    @computed_field
    @property
    def period(self) -> Period:
        return period_of(self.service_date)


class NfseEvent(Model):
    key: AccessKey
    code: Annotated[str, StringConstraints(pattern=r"^e\d{6}$")]


def _root(xml: bytes) -> etree._Element:
    # Government XML is still untrusted input: no entities, no network, no DTD.
    parser = etree.XMLParser(resolve_entities=False, no_network=True, dtd_validation=False, load_dtd=False)
    try:
        return etree.fromstring(xml, parser)
    except etree.XMLSyntaxError:
        raise BlockedError("XML do documento", "bem formado", "inválido", WHERE) from None


def _text(node: etree._Element, path: str) -> str | None:
    return node.findtext(path, namespaces=NAMESPACE)


def parse_nfse(xml: bytes) -> Nfse:
    root = _root(xml)
    info = root.find("n:infNFSe", NAMESPACE)
    try:
        return Nfse.model_validate(
            {
                "key": (info.get("Id", "") if info is not None else "").removeprefix("NFS"),
                "number": _text(root, "n:infNFSe/n:nNFSe"),
                "issuer": _text(root, "n:infNFSe/n:emit/n:CNPJ") or _text(root, "n:infNFSe/n:emit/n:CPF"),
                "situation": _text(root, "n:infNFSe/n:cStat"),
                "service_date": _text(root, ".//n:infDPS/n:dCompet"),
                "amount": _text(root, ".//n:infDPS/n:valores/n:vServPrest/n:vServ"),
                "client": _text(root, ".//n:infDPS/n:toma/n:xNome"),
                "description": _text(root, ".//n:infDPS/n:serv/n:cServ/n:xDescServ"),
                "service_code": _text(root, ".//n:infDPS/n:serv/n:cServ/n:cTribNac"),
                "iss_taxation": _text(root, ".//n:infDPS/n:valores/n:trib/n:tribMun/n:tribISSQN"),
            },
            strict=False,
        )
    except ValidationError as invalid:
        raise BlockedError.from_validation(invalid, WHERE) from invalid


def parse_event(xml: bytes) -> NfseEvent:
    root = _root(xml)
    codes = [etree.QName(e).localname for e in root.iter() if re.fullmatch(r"e\d{6}", etree.QName(e).localname)]
    try:
        return NfseEvent.model_validate({"key": _text(root, ".//n:chNFSe"), "code": codes[0] if codes else None})
    except ValidationError as invalid:
        raise BlockedError.from_validation(invalid, WHERE) from invalid


# --- Talking to the ADN -----------------------------------------------------


def connect(certificate: Certificate) -> httpx.Client:
    return httpx.Client(verify=certificate.ssl_context(), timeout=30)


class AdnClient:
    def __init__(self, http: httpx.Client, environment: Environment = Environment.PRODUCTION) -> None:
        self.http, self.environment, self.base = http, environment, ADN[environment]

    def batch(self, after_nsu: int) -> DfeBatch:
        """Documents from `after_nsu` onward. "Nothing new" arrives as HTTP 404 or 400 with a normal body."""
        response = self.http.get(f"{self.base}/contribuintes/DFe/{after_nsu}", params={"lote": "true"})
        if response.status_code not in (200, 400, 404):
            raise BlockedError("resposta do ADN", "HTTP 200", f"HTTP {response.status_code}", WHERE)
        try:
            batch = DfeBatch.model_validate_json(response.content)
        except ValidationError as invalid:
            raise BlockedError.from_validation(invalid, WHERE) from invalid
        if batch.environment != self.environment:
            raise BlockedError("ambiente do ADN", self.environment.value, batch.environment.value, WHERE)
        return batch

    def danfse(self, key: str) -> bytes:
        response = self.http.get(f"{self.base}/danfse/{key}")
        if response.status_code != 200 or not response.content.startswith(b"%PDF"):
            raise BlockedError("DANFSe", "PDF", f"HTTP {response.status_code}", WHERE)
        return response.content


class IssuedNfse(Wire):
    """SEFIN's answer to an accepted DPS."""

    key: Annotated[AccessKey, Field(alias="chaveAcesso")]
    dps_id: Annotated[str, Field(alias="idDps")]
    xml: Annotated[bytes, BeforeValidator(_unzip), Field(alias="nfseXmlGZipB64")]


def _rejections(body: object) -> list[str]:
    """SEFIN reports errors in several shapes; collect "code description" from whichever one came."""
    if isinstance(body, dict):
        found = body.get("erros") or body.get("erro") or body.get("Erros") or ([body] if "codigo" in body else [])
    else:
        found = body
    lines = []
    for item in found if isinstance(found, list) else []:
        if isinstance(item, dict):
            lowered = {str(k).lower(): v for k, v in item.items()}
            parts = (
                lowered.get("codigo"),
                lowered.get("descricao") or lowered.get("mensagem"),
                lowered.get("complemento"),
            )
            lines.append(" ".join(str(part) for part in parts if part))
    return lines


class SefinClient:
    def __init__(self, http: httpx.Client, environment: Environment = Environment.PRODUCTION) -> None:
        self.http, self.environment, self.base = http, environment, SEFIN[environment]

    def issue(self, signed_dps: bytes) -> IssuedNfse:
        """Send a signed DPS. Irreversible in production, and never retried: on doubt, ask `was_issued`."""
        payload = {"dpsXmlGZipB64": base64.b64encode(gzip.compress(signed_dps)).decode()}
        response = self.http.post(f"{self.base}/nfse", json=payload)
        try:
            body = response.json()
        except ValueError:
            raise BlockedError("resposta da SEFIN", "JSON", f"HTTP {response.status_code}", WHERE) from None
        if response.is_success and isinstance(body, dict) and body.get("chaveAcesso"):
            try:
                return IssuedNfse.model_validate(body)
            except ValidationError as invalid:
                raise BlockedError.from_validation(invalid, WHERE) from invalid
        reasons = _rejections(body) or [f"HTTP {response.status_code}"]
        raise BlockedError("emissão da NFS-e", "aceita", " | ".join(reasons), WHERE)

    def was_issued(self, dps_id: str) -> bool:
        """Whether a DPS already became an NFS-e: the way out after a send whose answer never arrived."""
        response = self.http.head(f"{self.base}/dps/{dps_id}")
        if response.status_code not in (200, 404):
            raise BlockedError("consulta da DPS", "HTTP 200 ou 404", f"HTTP {response.status_code}", WHERE)
        return response.status_code == 200


# --- Sync -------------------------------------------------------------------


class SyncReport(Model):
    issued: int = 0  # our invoices seen for the first time in this run
    events: int = 0  # cancellations and other events applied to our invoices
    ignored: int = 0  # documents that are not ours to account for
    pdfs_missing: int = 0  # DANFSe downloads that failed; retried on the next run


def _folder(period: str) -> str:
    return f"{period[:4]}/{period[5:]}"


def _conforms(nfse: Nfse, cfg: Config) -> bool:
    return (
        nfse.situation == AUTHORIZED
        and nfse.service_code == cfg.nfse.service_code.replace(".", "")
        and nfse.iss_taxation == EXPORT_OF_SERVICE
    )


def _file_nfse(s: Session, root: Path, document: DfeDocument, cfg: Config) -> bool:
    nfse = parse_nfse(document.xml)
    if nfse.issuer != cfg.cnpj:
        return False  # issued against us: an expense, not revenue
    if nfse.key != document.key:
        raise BlockedError("chave da NFS-e", document.key, nfse.key, WHERE)
    if any(known.key == nfse.key for known in db.invoices(s, nfse.period)):
        # Issued from here a moment ago: the invoice and SEFIN's own XML are already on file, and
        # the ADN's copy of the same document must neither count as new nor compete with it.
        return False
    db.save_invoice(
        s,
        db.Invoice(
            key=nfse.key,
            period=nfse.period,
            number=nfse.number,
            date=nfse.service_date,
            client=nfse.client,
            amount=nfse.amount,
            description=nfse.description,
            # Anything that is not the configured export-of-service profile needs human eyes.
            status="ISSUED" if _conforms(nfse, cfg) else "REVIEW",
        ),
    )
    db.save_document(
        s,
        root,
        f"{_folder(nfse.period)}/NFS-e-{nfse.number}.xml",
        document.xml,
        period=nfse.period,
        kind="NFS-e XML",
        origin="ADN",
        amount=nfse.amount,
    )
    return True


def _fetch_pdfs(s: Session, client: AdnClient, root: Path, periods: Iterable[str]) -> int:
    """Download the DANFSe of every invoice that still lacks one. The service is flaky, so failures only count."""
    missing = 0
    for period in periods:
        filed = {d.path for d in db.documents(s, period)}
        for invoice in db.invoices(s, period):
            path = f"{_folder(period)}/NFS-e-{invoice.number}.pdf"
            if path in filed:
                continue
            try:
                pdf = client.danfse(invoice.key)
            except BlockedError, httpx.HTTPError:
                missing += 1
                continue
            db.save_document(s, root, path, pdf, period=period, kind="NFS-e PDF", origin="ADN", amount=invoice.amount)
    return missing


def sync(s: Session, client: AdnClient, cfg: Config, root: Path, today: date) -> SyncReport:
    """Bring the local invoices up to date with the ADN. Safe to run any number of times."""
    issued = events = ignored = 0
    last = db.get_state(s, LAST_NSU)
    # PDFs are retried for the open month and the one being closed.
    periods: set[str] = {period_of(today), shift(period_of(today), -1)}
    for _ in range(MAX_BATCHES):
        batch = client.batch(last)
        if batch.status == "REJEICAO":
            first = batch.errors[0] if batch.errors else DfeError(code="?", description=None)
            raise BlockedError("consulta ao ADN", "aceita", f"{first.code} {first.description or ''}".strip(), WHERE)
        fresh = sorted((d for d in batch.documents if d.nsu > last), key=lambda d: d.nsu)
        if not fresh:
            break
        for document in fresh:
            if document.kind == "NFSE":
                if _file_nfse(s, root, document, cfg):
                    issued += 1
                    periods.add(parse_nfse(document.xml).period)
                else:
                    ignored += 1
            elif document.kind == "EVENTO":
                event = parse_event(document.xml)
                status = "CANCELLED" if event.code in CANCELLATIONS else "REVIEW"
                if db.set_invoice_status(s, event.key, status, event.code):
                    events += 1
                else:
                    ignored += 1
            else:
                ignored += 1
            # Advance only after the document is safely filed: a crash re-reads it, never skips it.
            last = document.nsu
            db.set_state(s, LAST_NSU, last)
    return SyncReport(issued=issued, events=events, ignored=ignored, pdfs_missing=_fetch_pdfs(s, client, root, periods))


def countable(invoices: Iterable[db.Invoice]) -> Decimal:
    """Revenue the invoices add up to. A cancelled invoice is not revenue."""
    return sum((i.amount for i in invoices if i.status != "CANCELLED"), Decimal(0))
