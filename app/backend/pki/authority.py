"""Checks and loading of a certificate authority"""
from typing import Optional

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa


class CaCheckError(Exception):
    """A CA file is not usable; `reason` is one of unreadable, not_a_ca, key_mismatch, key_usage, key_password"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def check_ca(cert: x509.Certificate, key: Optional[rsa.RSAPrivateKey] = None) -> None:
    """Raise CaCheckError unless `cert` is a CA that may sign certificates and CRLs (and matches `key` if given)"""
    try:
        basic = cert.extensions.get_extension_for_class(x509.BasicConstraints).value
    except x509.ExtensionNotFound:
        raise CaCheckError("not_a_ca") from None
    if not basic.ca:
        raise CaCheckError("not_a_ca")
    try:
        usage = cert.extensions.get_extension_for_class(x509.KeyUsage).value
    except x509.ExtensionNotFound:
        usage = None
    # A CA without keyUsage is unrestricted; one that has it must allow both signatures
    if usage is not None and not (usage.key_cert_sign and usage.crl_sign):
        raise CaCheckError("key_usage")
    if key is not None and key.public_key().public_numbers() != cert.public_key().public_numbers():
        raise CaCheckError("key_mismatch")


def load_ca_certificate(path: str) -> x509.Certificate:
    """Read a PEM CA certificate and check it"""
    try:
        with open(path, "rb") as handle:
            cert = x509.load_pem_x509_certificate(handle.read())
    except (OSError, ValueError):
        raise CaCheckError("unreadable") from None
    check_ca(cert)
    return cert


def load_ca(cert_path: str, key_path: str, password: str = "") -> tuple[x509.Certificate, rsa.RSAPrivateKey]:
    """Read a CA certificate and its private key (PEM, optionally encrypted) and check that they belong together"""
    cert = load_ca_certificate(cert_path)
    try:
        with open(key_path, "rb") as handle:
            data = handle.read()
    except OSError:
        raise CaCheckError("unreadable") from None
    try:
        key = serialization.load_pem_private_key(data, password.encode("utf-8") if password else None)
    except (TypeError, ValueError):
        # An encrypted key without or with a wrong password (cryptography raises these for both)
        raise CaCheckError("key_password") from None
    if not isinstance(key, rsa.RSAPrivateKey):
        raise CaCheckError("unreadable")
    check_ca(cert, key)
    return cert, key


def is_signed_by(cert: x509.Certificate, ca_cert: x509.Certificate) -> bool:
    """True when `ca_cert` issued `cert` (same issuer name and a valid signature)"""
    if cert.issuer != ca_cert.subject:
        return False
    try:
        cert.verify_directly_issued_by(ca_cert)
    except (ValueError, TypeError, InvalidSignature):
        return False
    return True


def sha256_fingerprint(cert: x509.Certificate) -> str:
    """Upper-case hex pairs joined by ':'"""
    digest = cert.fingerprint(hashes.SHA256()).hex().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))
