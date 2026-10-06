from typing import Dict, Optional, Tuple
import hashlib

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes

from app.backend.utils.logging_config import get_security_logger

_logger = get_security_logger()


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


async def parse_cn(dn: str) -> Optional[str]:
    """Parse CN from Distinguished Name.

    Handles both RFC 2253 format (CN=value,O=...) used by newer nginx
    and OpenSSL format (/CN=value/O=...) used by older nginx.
    """
    if not dn:
        return None

    # OpenSSL format: /CN=value/O=...
    if dn.startswith("/"):
        parts = [p.strip() for p in dn.split("/") if p]
        for p in parts:
            if p.startswith("CN="):
                return p[3:]

    # RFC 2253 format: CN=value,O=...
    parts = [p.strip() for p in dn.split(",")]
    for p in parts:
        if p.startswith("CN="):
            return p[3:]

    return None


async def hash_data(data: str) -> str:
    """Hash data using SHA256 with normalization.

    Strips colons and dashes before lowercasing so that
    'AB:CD:EF', 'ab-cd-ef' and 'abcdef' all produce the same hash.
    """
    normalized = data.replace(":", "").replace("-", "").strip().lower()
    return hashlib.sha256(normalized.encode('utf-8')).hexdigest()


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
        Tuple of (cn_hash, serial_hash, fingerprint_hash)
    """
    cn_hash = await hash_data(certificate_cn)
    serial_hash = await hash_data(certificate_serial)
    fingerprint_hash = await hash_data(certificate_fingerprint)

    return cn_hash, serial_hash, fingerprint_hash


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
