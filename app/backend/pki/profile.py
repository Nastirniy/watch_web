"""Certificate profiles: the client certificate and the client CA"""
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Optional

from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

LABEL_MAX_LENGTH = 64
BACKDATE = timedelta(minutes=5)
CLIENT_CA_KEY_SIZE = 4096


def normalize_label(value: str) -> Optional[str]:
    """Trimmed label, or None when empty, longer than LABEL_MAX_LENGTH or holding a control/format character"""
    label = value.strip()
    if not label or len(label) > LABEL_MAX_LENGTH:
        return None
    if any(unicodedata.category(char).startswith("C") for char in label):
        return None
    return label


def _now(now: Optional[datetime]) -> datetime:
    return now if now is not None else datetime.now(timezone.utc)


def _key_usage(digital_signature: bool = False, key_cert_sign: bool = False, crl_sign: bool = False) -> x509.KeyUsage:
    return x509.KeyUsage(
        digital_signature=digital_signature, content_commitment=False, key_encipherment=False,
        data_encipherment=False, key_agreement=False, key_cert_sign=key_cert_sign, crl_sign=crl_sign,
        encipher_only=False, decipher_only=False,
    )


def build_client_certificate(
    ca_cert: x509.Certificate,
    ca_key: rsa.RSAPrivateKey,
    public_key: rsa.RSAPublicKey,
    label: str,
    validity_days: int,
    now: Optional[datetime] = None,
) -> x509.Certificate:
    """A client certificate for `public_key`: CN is the label, clientAuth, no SAN, signed with SHA-256"""
    moment = _now(now)
    try:
        ca_ski = ca_cert.extensions.get_extension_for_class(x509.SubjectKeyIdentifier).value
    except x509.ExtensionNotFound:  # an authority made without extensions (the legacy root)
        ca_ski = x509.SubjectKeyIdentifier.from_public_key(ca_cert.public_key())
    return (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, label)]))
        .issuer_name(ca_cert.subject)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(moment - BACKDATE)
        .not_valid_after(moment + timedelta(days=validity_days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(_key_usage(digital_signature=True), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(public_key), critical=False)
        .add_extension(x509.AuthorityKeyIdentifier.from_issuer_subject_key_identifier(ca_ski), critical=False)
        .sign(ca_key, hashes.SHA256())
    )


def build_client_ca(
    common_name: str = "watch_web client CA",
    days: int = 3650,
    key_size: int = CLIENT_CA_KEY_SIZE,
    now: Optional[datetime] = None,
) -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
    """A self-signed client CA that can only sign leaves (pathlen 0), certificates and CRLs"""
    moment = _now(now)
    key = rsa.generate_private_key(public_exponent=65537, key_size=key_size)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(moment - BACKDATE)
        .not_valid_after(moment + timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(_key_usage(key_cert_sign=True, crl_sign=True), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    return cert, key
