"""Building, signing and validating a DPS, the declaration that becomes an NFS-e.

A new DPS is a copy of the last NFS-e this company issued, with only the fields that change
from one invoice to the next replaced. Everything fiscal (taker, service codes, foreign-trade
block, taxation) is inherited from a document the government already accepted.
"""

import copy
from datetime import date
from pathlib import Path
from typing import Annotated, Self

from lxml import etree
from pydantic import AwareDatetime, Field, StringConstraints, ValidationError, model_validator
from signxml import CanonicalizationMethod, DigestAlgorithm, SignatureMethod, XMLSigner, methods, namespaces

from .certificate import Certificate
from .core import BlockedError, Client, Config, Model, PositiveMoney, mismatch

WHERE = "Emissão de NFS-e"
NS = "http://www.sped.fazenda.gov.br/nfse"
N = {"n": NS}
SCHEMA = Path(__file__).parent / "schemas" / "nfse" / "DPS_v1.01.xsd"
EXPORT_OF_SERVICE = "3"


class DpsDraft(Model):
    """The only things that differ between two invoices to the same client."""

    production: bool  # False: the government's test environment, no fiscal effect
    number: Annotated[int, Field(gt=0, lt=10**15)]
    issued_at: AwareDatetime
    service_date: date
    description: Annotated[str, StringConstraints(min_length=1, max_length=2000, strip_whitespace=True)]
    amount: PositiveMoney
    foreign_amount: PositiveMoney

    @model_validator(mode="after")
    def _service_date_not_after_issue(self) -> Self:
        if self.service_date > self.issued_at.date():
            raise mismatch("data de competência", f"até {self.issued_at:%d/%m/%Y}", f"{self.service_date:%d/%m/%Y}")
        return self


class Template(Model):
    """What a new DPS inherits from the model invoice, for the user to read before confirming."""

    model_number: str
    series: str
    last_dps_number: int
    city_code: str
    service_code: str
    nbs_code: str


def _parse(xml: bytes) -> etree._Element:
    parser = etree.XMLParser(resolve_entities=False, no_network=True, load_dtd=False, remove_blank_text=True)
    try:
        return etree.fromstring(xml, parser)
    except etree.XMLSyntaxError:
        raise BlockedError("XML da nota modelo", "bem formado", "inválido", WHERE) from None


def _model_dps(model_nfse: bytes, cfg: Config) -> etree._Element:
    """The DPS inside an issued NFS-e, detached, unsigned, and checked against the configured profile."""
    found = _parse(model_nfse).find(".//n:DPS", N)
    if found is None:
        raise BlockedError("nota modelo", "NFS-e com DPS", "sem DPS", WHERE)
    dps = _parse(etree.tostring(found))  # reparse: a standalone tree with its own namespace declaration
    for signature in dps.findall(f"{{{namespaces.ds}}}Signature"):
        dps.remove(signature)
    for field, path, expected in (
        ("prestador da nota modelo", "n:infDPS/n:prest/n:CNPJ", cfg.cnpj),
        (
            "código do serviço da nota modelo",
            "n:infDPS/n:serv/n:cServ/n:cTribNac",
            cfg.nfse.service_code.replace(".", ""),
        ),
        ("tributação da nota modelo", "n:infDPS/n:valores/n:trib/n:tribMun/n:tribISSQN", EXPORT_OF_SERVICE),
    ):
        if (actual := dps.findtext(path, namespaces=N)) != expected:
            raise BlockedError(field, expected, actual, WHERE)
    return dps


def _get(dps: etree._Element, path: str) -> str:
    value = dps.findtext(path, namespaces=N)
    if value is None:
        raise BlockedError("nota modelo", f"campo {path.rsplit(':', 1)[-1]}", "ausente", WHERE)
    return value


def template(model_nfse: bytes, cfg: Config) -> Template:
    root = _parse(model_nfse)
    dps = _model_dps(model_nfse, cfg)
    return Template(
        model_number=root.findtext("n:infNFSe/n:nNFSe", default="?", namespaces=N),
        series=_get(dps, "n:infDPS/n:serie"),
        last_dps_number=int(_get(dps, "n:infDPS/n:nDPS")),
        city_code=_get(dps, "n:infDPS/n:cLocEmi"),
        service_code=_get(dps, "n:infDPS/n:serv/n:cServ/n:cTribNac"),
        nbs_code=_get(dps, "n:infDPS/n:serv/n:cServ/n:cNBS"),
    )


def client_from_model(model_nfse: bytes, cfg: Config) -> Client:
    """The client of an issued NFS-e, to seed the registry with someone the government already accepted."""
    dps = _model_dps(model_nfse, cfg)
    toma = "n:infDPS/n:toma/"
    try:
        return Client.model_validate(
            {
                "name": dps.findtext(toma + "n:xNome", namespaces=N),
                "country": dps.findtext(toma + "n:end/n:endExt/n:cPais", namespaces=N),
                "tax_id": dps.findtext(toma + "n:NIF", namespaces=N),
                "no_tax_id_reason": dps.findtext(toma + "n:cNaoNIF", namespaces=N),
                "postal_code": dps.findtext(toma + "n:end/n:endExt/n:cEndPost", namespaces=N),
                "city": dps.findtext(toma + "n:end/n:endExt/n:xCidade", namespaces=N),
                "region": dps.findtext(toma + "n:end/n:endExt/n:xEstProvReg", namespaces=N),
                "street": dps.findtext(toma + "n:end/n:xLgr", namespaces=N),
                "number": dps.findtext(toma + "n:end/n:nro", namespaces=N),
                "complement": dps.findtext(toma + "n:end/n:xCpl", namespaces=N),
                "district": dps.findtext(toma + "n:end/n:xBairro", namespaces=N),
                "currency": dps.findtext("n:infDPS/n:serv/n:comExt/n:tpMoeda", namespaces=N),
            }
        )
    except ValidationError as invalid:
        raise BlockedError.from_validation(invalid, WHERE) from invalid


def _client_block(client: Client) -> etree._Element:
    """The `toma` group, in the element order the official schema requires."""

    def node(parent: etree._Element, name: str, text: str | None = None) -> etree._Element:
        child = etree.SubElement(parent, f"{{{NS}}}{name}")
        child.text = text
        return child

    toma = etree.Element(f"{{{NS}}}toma", nsmap={None: NS})
    if client.tax_id is not None:
        node(toma, "NIF", client.tax_id)
    else:
        node(toma, "cNaoNIF", client.no_tax_id_reason)
    node(toma, "xNome", client.name)
    address = node(toma, "end")
    abroad = node(address, "endExt")
    node(abroad, "cPais", client.country)
    node(abroad, "cEndPost", client.postal_code)
    node(abroad, "xCidade", client.city)
    node(abroad, "xEstProvReg", client.region)
    node(address, "xLgr", client.street)
    node(address, "nro", client.number)
    if client.complement is not None:
        node(address, "xCpl", client.complement)
    node(address, "xBairro", client.district)
    return toma


def dps_id(city_code: str, cnpj: str, series: str, number: int) -> str:
    """ "DPS" + city (7) + registration type (2 = CNPJ) + CNPJ (14) + series (5) + number (15)."""
    return f"DPS{city_code}2{cnpj}{int(series):05d}{number:015d}"


def build(model_nfse: bytes, draft: DpsDraft, client: Client, cfg: Config) -> etree._Element:
    """An unsigned DPS: the model's, with the client and the per-invoice fields replaced."""
    dps = copy.deepcopy(_model_dps(model_nfse, cfg))
    info = dps.find("n:infDPS", N)
    if info is None:
        raise BlockedError("nota modelo", "infDPS", "ausente", WHERE)
    identifier = dps_id(_get(dps, "n:infDPS/n:cLocEmi"), cfg.cnpj, _get(dps, "n:infDPS/n:serie"), draft.number)
    info.set("Id", identifier)
    if not draft.production:
        # The test environment's municipal registry (CNC) has no record for the company, and it
        # refuses a municipal registration it cannot check (E0120). Production has the record.
        for registration in info.findall("n:prest/n:IM", N):
            parent = registration.getparent()
            if parent is not None:
                parent.remove(registration)
    previous = info.find("n:toma", N)
    if previous is None:
        raise BlockedError("nota modelo", "campo toma", "ausente", WHERE)
    info.replace(previous, _client_block(client))
    # Rule E0590: exporting with the service performed in Brazil requires the country where its
    # result took place. The model declares the client's country there; keep doing the same.
    result_country = info.find("n:valores/n:trib/n:tribMun/n:cPaisResult", N)
    if result_country is not None:
        result_country.text = client.country
    for path, value in (
        ("n:serv/n:comExt/n:tpMoeda", client.currency),
        ("n:tpAmb", "1" if draft.production else "2"),
        ("n:dhEmi", draft.issued_at.replace(microsecond=0).isoformat()),
        ("n:nDPS", str(draft.number)),
        ("n:dCompet", draft.service_date.isoformat()),
        ("n:serv/n:cServ/n:xDescServ", draft.description),
        ("n:serv/n:comExt/n:vServMoeda", str(draft.foreign_amount)),
        ("n:valores/n:vServPrest/n:vServ", str(draft.amount)),
    ):
        target = info.find(path, N)
        if target is None:
            raise BlockedError("nota modelo", f"campo {path.rsplit(':', 1)[-1]}", "ausente", WHERE)
        target.text = value
    return dps


def sign(dps: etree._Element, certificate: Certificate) -> bytes:
    """Enveloped signature over infDPS, in the exact shape the national issuer itself produces."""
    info = dps.find("n:infDPS", N)
    if info is None:
        raise BlockedError("DPS", "infDPS", "ausente", WHERE)
    signer = XMLSigner(
        method=methods.enveloped,
        signature_algorithm=SignatureMethod.RSA_SHA256,
        digest_algorithm=DigestAlgorithm.SHA256,
        c14n_algorithm=CanonicalizationMethod.EXCLUSIVE_XML_CANONICALIZATION_1_0_WITH_COMMENTS,
    )
    # No prefix on the Signature element: the government rejects namespace prefixes (E1228).
    signer.namespaces = {None: namespaces.ds}  # ty: ignore[invalid-assignment]  (signxml's documented idiom)
    signed = signer.sign(dps, key=certificate.key, cert=[certificate.cert], reference_uri=f"#{info.get('Id')}")
    # These exact bytes are what gets sent: re-serializing later would break the digest.
    return etree.tostring(signed, xml_declaration=True, encoding="UTF-8")


def validate(xml: bytes) -> None:
    """Refuse anything the official schema would refuse, before it leaves the machine."""
    schema = etree.XMLSchema(etree.parse(SCHEMA))
    document = _parse(xml)
    if not schema.validate(document):
        error = schema.error_log[0]
        raise BlockedError("esquema oficial da DPS", "válido", f"linha {error.line}: {error.message}", WHERE)
