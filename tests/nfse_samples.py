"""Synthetic NFS-e documents shaped like the real ones the ADN distributes. No real taxpayer data."""

import base64
import gzip
import json

OWN_CNPJ = "11444777000161"  # matches config.toml
OTHER_CNPJ = "11222333000181"
NS = "http://www.sped.fazenda.gov.br/nfse"


def key(number: int, issuer: str = OWN_CNPJ) -> str:
    return f"21113002{2}{issuer}{number:013d}2609{number:09d}7"[:50]


def nfse_xml(
    number: int = 1,
    *,
    issuer: str = OWN_CNPJ,
    amount: str = "13731.88",
    service_date: str = "2026-09-29",
    service_code: str = "170102",
    iss_taxation: str = "3",
    description: str = "Pagamento referente a serviços prestados em Agosto de 2026",
) -> bytes:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<NFSe xmlns="{NS}" versao="1.01">
  <infNFSe Id="NFS{key(number, issuer)}">
    <nNFSe>{number}</nNFSe>
    <cStat>100</cStat>
    <emit><CNPJ>{issuer}</CNPJ><xNome>EMPRESA DE TESTE</xNome></emit>
    <valores><vLiq>{amount}</vLiq></valores>
    <DPS xmlns="{NS}" versao="1.01">
      <infDPS Id="DPS2111300{2}{issuer}00001{number:015d}">
        <tpAmb>1</tpAmb>
        <dCompet>{service_date}</dCompet>
        <prest><CNPJ>{issuer}</CNPJ></prest>
        <toma><cNaoNIF>2</cNaoNIF><xNome>Acme Research Foundation</xNome></toma>
        <serv>
          <cServ><cTribNac>{service_code}</cTribNac><xDescServ>{description}</xDescServ></cServ>
        </serv>
        <valores>
          <vServPrest><vServ>{amount}</vServ></vServPrest>
          <trib><tribMun><tribISSQN>{iss_taxation}</tribISSQN><tpRetISSQN>1</tpRetISSQN></tribMun></trib>
        </valores>
      </infDPS>
    </DPS>
  </infNFSe>
</NFSe>""".encode()


def event_xml(invoice_key: str, code: str = "e101101") -> bytes:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<evento xmlns="{NS}" versao="1.01">
  <infEvento Id="EVT{invoice_key}">
    <pedRegEvento versao="1.01">
      <infPedReg Id="PRE{invoice_key}">
        <tpAmb>1</tpAmb>
        <chNFSe>{invoice_key}</chNFSe>
        <{code}><xDesc>Evento</xDesc></{code}>
      </infPedReg>
    </pedRegEvento>
  </infEvento>
</evento>""".encode()


def zipped(xml: bytes) -> str:
    return base64.b64encode(gzip.compress(xml)).decode()


def batch(*documents: tuple[int, str, str, bytes], environment: str = "PRODUCAO") -> bytes:
    """An ADN response. Each document is (nsu, kind, key, xml); none means "nothing new"."""
    return json.dumps(
        {
            "StatusProcessamento": "DOCUMENTOS_LOCALIZADOS" if documents else "NENHUM_DOCUMENTO_LOCALIZADO",
            "LoteDFe": [
                {"NSU": nsu, "ChaveAcesso": k, "TipoDocumento": kind, "ArquivoXml": zipped(xml), "TipoEvento": None}
                for nsu, kind, k, xml in documents
            ]
            or None,
            "Alertas": [],
            "Erros": [] if documents else [{"Codigo": "E2215", "Descricao": "Nenhum documento localizado"}],
            "TipoAmbiente": environment,
            "VersaoAplicativo": "1.0.0.0",
        }
    ).encode()


def full_nfse_xml(number: int = 1, dps_number: int = 4, issuer: str = OWN_CNPJ) -> bytes:
    """A complete issued NFS-e (schema-valid DPS inside), usable as the model for a new one."""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<NFSe xmlns="{NS}" versao="1.01"><infNFSe Id="NFS{key(number, issuer)}"><nNFSe>{number}</nNFSe><cStat>100</cStat>
<emit><CNPJ>{issuer}</CNPJ><xNome>EMPRESA DE TESTE</xNome></emit><valores><vLiq>13731.88</vLiq></valores>
<DPS xmlns="{NS}" versao="1.01"><infDPS Id="DPS21113002{issuer}00001{dps_number:015d}">
<tpAmb>1</tpAmb><dhEmi>2026-09-29T16:27:30-03:00</dhEmi><verAplic>1.00</verAplic><serie>1</serie>
<nDPS>{dps_number}</nDPS><dCompet>2026-09-29</dCompet><tpEmit>1</tpEmit><cLocEmi>2111300</cLocEmi>
<prest><CNPJ>{issuer}</CNPJ><IM>1234567890</IM>
<regTrib><opSimpNac>3</opSimpNac><regApTribSN>1</regApTribSN><regEspTrib>0</regEspTrib></regTrib></prest>
<toma><cNaoNIF>2</cNaoNIF><xNome>Acme Research Foundation</xNome><end>
<endExt><cPais>IT</cPais><cEndPost>000</cEndPost><xCidade>Lecce</xCidade><xEstProvReg>000</xEstProvReg></endExt>
<xLgr>via Roma</xLgr><nro>5</nro><xBairro>Centro</xBairro></end></toma>
<serv><locPrest><cLocPrestacao>2111300</cLocPrestacao></locPrest>
<cServ><cTribNac>170102</cTribNac><xDescServ>Pagamento referente a serviços prestados em Agosto de 2026</xDescServ>
<cNBS>118061000</cNBS></cServ>
<comExt><mdPrestacao>4</mdPrestacao><vincPrest>0</vincPrest><tpMoeda>220</tpMoeda><vServMoeda>2634.06</vServMoeda>
<mecAFComexP>01</mecAFComexP><mecAFComexT>01</mecAFComexT><movTempBens>1</movTempBens><mdic>0</mdic></comExt></serv>
<valores><vServPrest><vServ>13731.88</vServ></vServPrest><trib>
<tribMun><tribISSQN>3</tribISSQN><cPaisResult>IT</cPaisResult><tpRetISSQN>1</tpRetISSQN></tribMun>
<totTrib><pTotTribSN>6.00</pTotTribSN></totTrib></trib></valores>
</infDPS></DPS></infNFSe></NFSe>""".encode()
