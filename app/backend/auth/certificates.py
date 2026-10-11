from typing import Dict, List, Optional, Tuple
import hashlib
import string
import unicodedata

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes

from sqlalchemy import and_, or_

from app.backend.utils.logging_config import get_security_logger

_logger = get_security_logger()

# users.pki_certificate_cn_hash_scheme: the rule that made the stored name hash
CN_SCHEME_LEGACY = 1  # hash_data; certificates stored before migration 019 keep it
CN_SCHEME_CURRENT = 2  # hash_cn; every certificate stored from now on


class CertificateParser:
    """Service for parsing X.509 PKI certificates"""

    @staticmethod
    def parse_certificate(cert_data: bytes) -> Dict[str, str]:
        """
        Parse X.509 certificate and extract required fields

        Args:
            cert_data: Certificate data in PEM or DER format

        Returns:
            Dictionary containing:
                - pki_certificate_cn: Common Name from subject (or an identifier built from the subject)
                - pki_certificate_serial: Serial number as hex string
                - pki_certificate_fingerprint: SHA1 fingerprint (matches nginx $ssl_client_fingerprint)

        Raises:
            ValueError: cert_data is not a PEM or DER X.509 certificate (cryptography's own error). Any other
            exception is a bug and is not disguised as an invalid certificate.
        """
        try:
            cert = x509.load_pem_x509_certificate(cert_data, default_backend())
            _logger.debug("Certificate loaded as PEM format")
        except ValueError:
            # Not PEM: DER, or cryptography's ValueError when it is no certificate at all
            cert = x509.load_der_x509_certificate(cert_data, default_backend())
            _logger.debug("Certificate loaded as DER format")

        # Common Name; without one, an identifier from other subject fields, else the full subject DN
        cn_attributes = cert.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)
        cn = cn_attributes[0].value if cn_attributes else None
        if not cn:
            subject_parts = [
                attributes[0].value
                for oid in (x509.oid.NameOID.ORGANIZATION_NAME, x509.oid.NameOID.STATE_OR_PROVINCE_NAME)
                if (attributes := cert.subject.get_attributes_for_oid(oid))
            ]
            cn = " - ".join(subject_parts) if subject_parts else str(cert.subject)
            _logger.debug(f"CN not found, identifier from the subject: {cn}")

        serial_number = format(cert.serial_number, 'x').lower()
        fingerprint = cert.fingerprint(hashes.SHA1()).hex().lower()
        _logger.debug(f"Certificate CN={cn}, serial={serial_number}, fingerprint={fingerprint[:16]}...")

        return {
            "pki_certificate_cn": str(cn),
            "pki_certificate_serial": str(serial_number),
            "pki_certificate_fingerprint": str(fingerprint)
        }


def _split_unescaped(text: str, separator: str) -> List[str]:
    """text split on every separator not escaped with a backslash; escapes stay in the parts"""
    parts, current, index = [], [], 0
    while index < len(text):
        if text[index] == "\\" and index + 1 < len(text):
            current.append(text[index:index + 2])
            index += 2
            continue
        if text[index] == separator:
            parts.append("".join(current))
            current = []
        else:
            current.append(text[index])
        index += 1
    parts.append("".join(current))
    return parts


def _unescape_rfc2253(value: str) -> Optional[str]:
    """An attribute value of an RFC 2253 / 4514 DN: \\XX is one byte (OpenSSL writes every byte of a character above
    0x7F so), \\<char> the character itself. None when the bytes are not UTF-8."""
    raw = bytearray()
    index = 0
    while index < len(value):
        char = value[index]
        if char == "\\" and index + 1 < len(value):
            pair = value[index + 1:index + 3]
            if len(pair) == 2 and all(c in string.hexdigits for c in pair):
                raw.append(int(pair, 16))
                index += 3
                continue
            char = value[index + 1]
            index += 1
        raw.extend(char.encode("utf-8"))
        index += 1
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


async def parse_cn(dn: str) -> Optional[str]:
    """The CN of a subject DN as nginx forwards it ($ssl_client_s_dn).

    RFC 2253 form (nginx 1.11.6 and later, OpenSSL XN_FLAG_RFC2253): RDNs separated by unescaped ',', attributes
    of one RDN by unescaped '+'; the value is unescaped (\\D0\\86 is the UTF-8 of U+0406, \\, a comma). The legacy form
    /C=UA/O=.../CN=value of older nginx is read as before.
    """
    if not dn:
        return None

    if dn.startswith("/"):
        for part in (p.strip() for p in dn.split("/") if p):
            if part.startswith("CN="):
                return part[3:]
        return None

    # RFC 2253 lists the RDNs last first (the attributes of one RDN keep their order): read in certificate order
    # the first CN is the one CertificateParser stores.
    values = [
        value
        for rdn in reversed(_split_unescaped(dn, ","))
        for attribute in _split_unescaped(rdn, "+")
        for name, separator, value in [attribute.partition("=")]
        if separator and name.strip().upper() == "CN"
    ]
    return _unescape_rfc2253(values[0]) if values else None


async def hash_data(data: str) -> str:
    """Hash data using SHA256 with normalization.

    Strips colons and dashes before lowercasing so that
    'AB:CD:EF', 'ab-cd-ef' and 'abcdef' all produce the same hash.
    """
    normalized = data.replace(":", "").replace("-", "").strip().lower()
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()


async def hash_cn(common_name: str) -> str:
    """Scheme 2 hash of a certificate name: Unicode NFC and surrounding whitespace stripped, nothing else.

    Case, dashes and colons are part of a name ('Post-Kyiv' and 'postkyiv' are two certificates), unlike a serial
    or a fingerprint (hash_data).
    """
    return hashlib.sha256(unicodedata.normalize("NFC", common_name).strip().encode("utf-8")).hexdigest()


async def serial_hashes(certificate_serial: str) -> set[str]:
    """The hashes a stored serial may have: as written and without leading zeros.

    nginx ($ssl_client_serial, OpenSSL i2a_ASN1_INTEGER) writes two hex digits per byte, so serial 0x1 arrives
    as "01", while CertificateParser stores format(serial, 'x'), "1". A serial typed into the configuration
    (settings.admin) may have either form, so the login matches both.
    """
    normalized = certificate_serial.replace(":", "").replace("-", "").strip().lower()
    return {await hash_data(normalized), await hash_data(normalized.lstrip("0") or "0")}


async def validate_pki_certificate_data(
    certificate_cn: str,
    certificate_serial: str,
    certificate_fingerprint: str
) -> bool:
    """
    Validate that PKI certificate data is present and not empty

    Args:
        certificate_cn: Certificate Common Name
        certificate_serial: Certificate Serial Number
        certificate_fingerprint: Certificate Fingerprint

    Returns:
        True if all fields are valid, False otherwise
    """
    return all([certificate_cn, certificate_serial, certificate_fingerprint])


async def hash_pki_certificate_data(
    certificate_cn: str,
    certificate_serial: str,
    certificate_fingerprint: str
) -> Tuple[str, str, str]:
    """
    Hash PKI certificate data using SHA256

    Args:
        certificate_cn: Certificate Common Name
        certificate_serial: Certificate Serial Number
        certificate_fingerprint: Certificate Fingerprint

    Returns:
        Tuple of (cn_hash, serial_hash, fingerprint_hash); the name hash is scheme 2 (CN_SCHEME_CURRENT), the
        rule of every certificate stored now
    """
    cn_hash = await hash_cn(certificate_cn)
    serial_hash = await hash_data(certificate_serial)
    fingerprint_hash = await hash_data(certificate_fingerprint)

    return cn_hash, serial_hash, fingerprint_hash


async def cn_hash_matches(certificate_cn: str):
    """SQL condition: the user row holds this name under the rule of its own scheme (scheme 1 rows were hashed
    with hash_data, scheme 2 rows with hash_cn)."""
    from app.backend.models.user import User  # models load auth.sessions, which imports this module
    return or_(
        and_(User.pki_certificate_cn_hash_scheme == CN_SCHEME_LEGACY,
             User.pki_certificate_cn_hash == await hash_data(certificate_cn)),
        and_(User.pki_certificate_cn_hash_scheme == CN_SCHEME_CURRENT,
             User.pki_certificate_cn_hash == await hash_cn(certificate_cn)),
    )


async def create_pki_auth_audit_details(
    certificate_cn: str,
    certificate_serial: str,
    fingerprint_hash: str
) -> Dict[str, str]:
    """
    Create details dictionary for PKI authentication audit log

    Args:
        certificate_cn: Certificate Common Name
        certificate_serial: Certificate Serial Number
        fingerprint_hash: Hashed certificate fingerprint

    Returns:
        Dictionary with certificate details for audit log
    """
    return {
        "certificate_cn": certificate_cn,
        "certificate_serial": certificate_serial,
        "certificate_fingerprint_hash": fingerprint_hash[:16]
    }
