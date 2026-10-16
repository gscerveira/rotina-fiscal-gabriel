import ssl
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

from fiscal import certificate
from fiscal.core import BlockedError

CNPJ = "11444777000161"
TODAY = date(2026, 10, 7)
HOLDER = f"ACME LTDA:{CNPJ}"
PASSWORD = "test-only"  # noqa: S105 - throwaway certificate generated below


def make_pfx(folder: Path, common_name: str = HOLDER, days: int = 365) -> Path:
    """A self-signed stand-in for an e-CNPJ A1. Nothing here touches the real certificate."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    start = datetime(2026, 5, 18, tzinfo=UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(start)
        .not_valid_after(start + timedelta(days=days))
        .sign(key, hashes.SHA256())
    )
    path = folder / "test.pfx"
    path.write_bytes(
        pkcs12.serialize_key_and_certificates(
            b"test", key, cert, None, serialization.BestAvailableEncryption(PASSWORD.encode())
        )
    )
    return path


def test_loads_a_valid_ecnpj(tmp_path: Path):
    loaded = certificate.load(make_pfx(tmp_path), PASSWORD, CNPJ, TODAY)
    assert (loaded.info.holder, loaded.info.cnpj) == ("ACME LTDA", CNPJ)
    assert loaded.info.days_left(TODAY) == 223
    assert "PrivateKey" not in repr(loaded)


def test_mutual_tls_context_leaves_no_key_on_disk(tmp_path: Path):
    loaded = certificate.load(make_pfx(tmp_path), PASSWORD, CNPJ, TODAY)
    context = loaded.ssl_context()
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert [p.name for p in tmp_path.iterdir()] == ["test.pfx"]


@pytest.mark.parametrize(
    ("common_name", "password", "cnpj", "today", "field"),
    [
        (HOLDER, "wrong", CNPJ, TODAY, "senha do certificado"),
        (HOLDER, PASSWORD, "54750193000127", TODAY, "CNPJ do certificado"),
        (HOLDER, PASSWORD, CNPJ, date(2027, 6, 1), "validade do certificado"),
        (HOLDER, PASSWORD, CNPJ, date(2026, 1, 1), "validade do certificado"),
        ("JOAO DA SILVA:12345678909", PASSWORD, CNPJ, TODAY, "tipo do certificado"),
    ],
)
def test_refuses_anything_but_this_company_valid_ecnpj(
    tmp_path: Path, common_name: str, password: str, cnpj: str, today: date, field: str
):
    with pytest.raises(BlockedError) as blocked:
        certificate.load(make_pfx(tmp_path, common_name), password, cnpj, today)
    assert blocked.value.field == field


def test_missing_file_and_missing_password_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    with pytest.raises(BlockedError) as blocked:
        certificate.load(tmp_path / "nope.pfx", PASSWORD, CNPJ, TODAY)
    assert blocked.value.field == "arquivo do certificado"

    monkeypatch.setattr(certificate.keyring, "get_password", lambda *_: None)
    with pytest.raises(BlockedError) as blocked:
        certificate.stored_password()
    assert blocked.value.field == "senha do certificado"
