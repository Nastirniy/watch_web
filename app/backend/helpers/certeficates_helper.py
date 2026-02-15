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
                - pki_certificate_cn: Common Name from subject
                - pki_certificate_serial: Serial number as hex string
                - pki_certificate_fingerprint: SHA256 fingerprint

        Raises:
            ValueError: If certificate cannot be parsed or required fields are missing
        """
        try:
            # Try to load as PEM first
            try:
                cert = x509.load_pem_x509_certificate(cert_data, default_backend())
                _logger.debug("Certificate loaded as PEM format")
            except ValueError:
                # If PEM fails, try DER
                cert = x509.load_der_x509_certificate(cert_data, default_backend())
                _logger.debug("Certificate loaded as DER format")

            # Extract Common Name (CN) from Subject, or use alternative identifier
            cn = None
            try:
                cn_attributes = cert.subject.get_attributes_for_oid(x509.oid.NameOID.COMMON_NAME)
                if cn_attributes:
                    cn = cn_attributes[0].value
                    _logger.debug(f"Extracted CN: {cn}")
            except Exception as e:
                _logger.warning(f"Could not extract CN: {str(e)}")

            # If CN is not available, construct an identifier from other subject fields
            if not cn:
                _logger.debug("CN not found, constructing identifier from subject fields")
                subject_parts = []

                # Try to get Organization Name
                try:
                    org_attrs = cert.subject.get_attributes_for_oid(x509.oid.NameOID.ORGANIZATION_NAME)
                    if org_attrs:
                        subject_parts.append(org_attrs[0].value)
                except Exception:
                    pass

                # Try to get State/Province
                try:
                    state_attrs = cert.subject.get_attributes_for_oid(x509.oid.NameOID.STATE_OR_PROVINCE_NAME)
                    if state_attrs:
                        subject_parts.append(state_attrs[0].value)
                except Exception:
                    pass

                # If we have parts, join them; otherwise use full subject DN
                if subject_parts:
                    cn = " - ".join(subject_parts)
                    _logger.debug(f"Constructed identifier from subject parts: {cn}")
                else:
                    # Use full subject DN as fallback
                    cn = str(cert.subject)
                    _logger.debug(f"Using full subject DN as identifier: {cn}")

            # Extract Serial Number
            serial_number = format(cert.serial_number, 'x').lower()
            _logger.debug(f"Extracted serial number: {serial_number}")

            # Generate SHA1 fingerprint (matches nginx $ssl_client_fingerprint)
            fingerprint = cert.fingerprint(hashes.SHA1()).hex().lower()
            _logger.debug(f"Generated fingerprint: {fingerprint[:16]}...")

            return {
                "pki_certificate_cn": str(cn),
                "pki_certificate_serial": str(serial_number),
                "pki_certificate_fingerprint": str(fingerprint)
            }

        except Exception as e:
            _logger.error(f"Certificate parsing error: {str(e)}")
            raise ValueError(f"Invalid certificate format or content: {str(e)}")

    @staticmethod
    def validate_certificate(cert_data: bytes) -> bool:
        """
        Validate that certificate data is a valid X.509 certificate

        Args:
            cert_data: Certificate data to validate

        Returns:
            True if valid, False otherwise
        """
        try:
            CertificateParser.parse_certificate(cert_data)
            return True
        except Exception:
            return False

    # @staticmethod
    # def get_certificate_info(cert_data: bytes) -> Optional[Dict[str, str]]:
    #     """
    #     Get additional certificate information for logging/debugging
    #
    #     Args:
    #         cert_data: Certificate data
    #
    #     Returns:
    #         Dictionary with certificate details or None if parsing fails
    #     """
    #     try:
    #         try:
    #             cert = x509.load_pem_x509_certificate(cert_data, default_backend())
    #         except ValueError:
    #             cert = x509.load_der_x509_certificate(cert_data, default_backend())
    #
    #         # Extract subject fields
    #         subject_fields = {}
    #         for attribute in cert.subject:
    #             subject_fields[attribute.oid._name] = attribute.value
    #
    #         # Extract issuer fields
    #         issuer_fields = {}
    #         for attribute in cert.issuer:
    #             issuer_fields[attribute.oid._name] = attribute.value
    #
    #         return {
    #             "subject": str(cert.subject),
    #             "issuer": str(cert.issuer),
    #             "serial_number": format(cert.serial_number, 'x').lower(),
    #             "not_valid_before": cert.not_valid_before_utc.isoformat(),
    #             "not_valid_after": cert.not_valid_after_utc.isoformat(),
    #             "version": cert.version.name,
    #             "subject_fields": subject_fields,
    #             "issuer_fields": issuer_fields
    #         }
    #     except Exception as e:
    #         _logger.error(f"Error getting certificate info: {str(e)}")
            return None


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
