"""The package of a user: key, password and the .p12 file"""
import re
import secrets

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12

from app.backend.enums.general import P12Encryption

# No 0 O 1 l I
PASSWORD_ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_GROUPS, _GROUP_SIZE = 4, 5
_KDF_ROUNDS = 50000


def new_password() -> str:
    """20 characters in 4 groups of 5 joined by '-'"""
    return "-".join(
        "".join(secrets.choice(PASSWORD_ALPHABET) for _ in range(_GROUP_SIZE)) for _ in range(_GROUPS)
    )


def generate_key(key_size: int) -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=key_size)


def build_p12(key: rsa.RSAPrivateKey, cert: x509.Certificate, label: str, password: str,
              encryption: P12Encryption) -> bytes:
    """A .p12 with the key and the certificate under `label` as friendly name, and no CA certificate"""
    if encryption == P12Encryption.AES256:
        algorithm, mac = pkcs12.PBES.PBESv2SHA256AndAES256CBC, hashes.SHA256()
    else:
        algorithm, mac = pkcs12.PBES.PBESv1SHA1And3KeyTripleDESCBC, hashes.SHA1()
    encryption_algorithm = (
        serialization.PrivateFormat.PKCS12.encryption_builder()
        .kdf_rounds(_KDF_ROUNDS)
        .key_cert_algorithm(algorithm)
        .hmac_hash(mac)
        .build(password.encode("utf-8"))
    )
    return pkcs12.serialize_key_and_certificates(label.encode("utf-8"), key, cert, None, encryption_algorithm)


def p12_filename(label: str, certificate_id: int) -> str:
    """ASCII letters and digits of the label, every run of other characters as one '-'; certificate-<id> if none"""
    stem = re.sub(r"[^A-Za-z0-9]+", "-", label).strip("-")
    return f"{stem or f'certificate-{certificate_id}'}.p12"
