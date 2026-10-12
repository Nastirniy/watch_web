"""Throwaway certificate authorities and leaves for the PKI tests (never a real key)"""
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Optional

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app.backend.pki.package import generate_key
from app.backend.pki.profile import build_client_ca, build_client_certificate


@lru_cache(maxsize=1)
def client_ca() -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
    return build_client_ca(key_size=2048)


@lru_cache(maxsize=1)
def legacy_root() -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
    """A self-signed root without extensions, like the one gencert.sh makes"""
    key = generate_key(2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "legacy root")])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=3650))
        .sign(key, hashes.SHA256())
    )
    return cert, key


def make_leaf(ca: tuple, label: str = "Test device", days: int = 30,
              now: Optional[datetime] = None) -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
    key = generate_key(2048)
    return build_client_certificate(ca[0], ca[1], key.public_key(), label, days, now), key


def pem(obj) -> bytes:
    """PEM of a certificate or a CRL"""
    return obj.public_bytes(serialization.Encoding.PEM)


def write_ca_files(directory: Path, ca: tuple, password: str = "") -> tuple[Path, Path]:
    """Write the certificate and the (optionally encrypted) key of a CA; returns (cert_path, key_path)"""
    encryption = (serialization.BestAvailableEncryption(password.encode()) if password
                  else serialization.NoEncryption())
    cert_path, key_path = directory / "ca.crt", directory / "ca.key"
    cert_path.write_bytes(pem(ca[0]))
    key_path.write_bytes(ca[1].private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, encryption))
    return cert_path, key_path


class DownTransport(httpx.AsyncBaseTransport):
    """A signer that does not answer (socket gone, service stopped)"""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("signer is down", request=request)
