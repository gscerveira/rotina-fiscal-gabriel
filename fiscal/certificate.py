"""The e-CNPJ A1 certificate: loading, sanity checks and the mutual-TLS context.

The .pfx stays where the user put it (it is already encrypted). Its password lives only in the
system keyring, and the private key never touches the disk unencrypted.
"""

import re
import secrets
import ssl
import tempfile
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import keyring
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID
from pydantic import computed_field

from .core import BlockedError, Cnpj, Model, NonEmpty

KEYRING_SERVICE = "rotina-fiscal"
KEYRING_ENTRY = "certificate"
WHERE = "Certificado digital"
EXPIRY_WARNING_DAYS = 30


class CertificateInfo(Model):
    """What the rest of the program may know about the certificate. No key material."""

    holder: NonEmpty
    cnpj: Cnpj
    not_before: datetime
    not_after: datetime

    def days_left(self, today: date) -> int:
        return (self.not_after.date() - today).days

    @computed_field
    @property
    def expires_on(self) -> date:
        return self.not_after.date()


@dataclass(frozen=True, repr=False)  # repr=False: a stray log line must never print the key object
class Certificate:
    info: CertificateInfo
    key: RSAPrivateKey
    cert: x509.Certificate
    chain: tuple[x509.Certificate, ...]

    def ssl_context(self) -> ssl.SSLContext:
        """Client context for mutual TLS. The key is written only encrypted, to a file deleted on return."""
        passphrase = secrets.token_urlsafe(32).encode()
        pem = self.key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.BestAvailableEncryption(passphrase),
        )
        for certificate in (self.cert, *self.chain):
            pem += certificate.public_bytes(serialization.Encoding.PEM)
        context = ssl.create_default_context()
        with tempfile.NamedTemporaryFile(suffix=".pem") as file:
            file.write(pem)
            file.flush()
            context.load_cert_chain(file.name, password=passphrase)
        return context

    def cert_der(self) -> bytes:
        return self.cert.public_bytes(serialization.Encoding.DER)


def stored_password() -> str:
    password = keyring.get_password(KEYRING_SERVICE, KEYRING_ENTRY)
    if not password:
        raise BlockedError("senha do certificado", "guardada no keyring do sistema", "ausente", WHERE)
    return password


def store_password(password: str) -> None:
    keyring.set_password(KEYRING_SERVICE, KEYRING_ENTRY, password)


def load(path: Path, password: str, expected_cnpj: str, today: date) -> Certificate:
    """Open the .pfx and refuse it unless it is a valid e-CNPJ for exactly this company, today."""
    if not path.is_file():
        raise BlockedError("arquivo do certificado", str(path), "não encontrado", WHERE)
    try:
        key, cert, chain = pkcs12.load_key_and_certificates(path.read_bytes(), password.encode())
    except ValueError:
        raise BlockedError("senha do certificado", "a que abre o arquivo .pfx", "recusada", WHERE) from None
    if not isinstance(key, RSAPrivateKey) or cert is None:
        raise BlockedError("conteúdo do certificado", "chave RSA e certificado", "incompleto", WHERE)

    # ICP-Brasil e-CNPJ: the subject common name is "<HOLDER>:<CNPJ>".
    common_name = str(cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value)
    match = re.fullmatch(r"(.+):([0-9A-Z]{14})", common_name)
    if match is None:
        raise BlockedError("tipo do certificado", "e-CNPJ (nome:CNPJ)", common_name, WHERE)
    info = CertificateInfo(
        holder=match[1], cnpj=match[2], not_before=cert.not_valid_before_utc, not_after=cert.not_valid_after_utc
    )
    if info.cnpj != expected_cnpj:
        raise BlockedError("CNPJ do certificado", expected_cnpj, info.cnpj, WHERE)
    if not info.not_before.date() <= today <= info.expires_on:
        raise BlockedError(
            "validade do certificado",
            f"cobrindo {today:%d/%m/%Y}",
            f"{info.not_before:%d/%m/%Y} a {info.not_after:%d/%m/%Y}",
            WHERE,
        )
    return Certificate(info=info, key=key, cert=cert, chain=tuple(chain))
