"""
Security utilities for authentication, authorization, and cryptography.
Handles JWT tokens, PKI certificates, password hashing, and session management.
"""

import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Union, Tuple, List

from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from jose import JWTError, jwt
from loguru import logger
from passlib.context import CryptContext
from sqlalchemy.ext.asyncio import AsyncSession

from backend.core.config.config import settings
from backend.database.crud.user import user as user_crud
from backend.models.user import User

# Password hashing context
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# JWT token types
ACCESS_TOKEN_TYPE = "access"
REFRESH_TOKEN_TYPE = "refresh"


def create_access_token(
        subject: Union[str, Any],
        expires_delta: Optional[timedelta] = None,
        additional_claims: Optional[Dict[str, Any]] = None
) -> str:
    """
    Create a new access token.

    Args:
        subject: Token subject (usually user ID)
        expires_delta: Token expiration time delta
        additional_claims: Additional claims to include in token

    Returns:
        JWT access token string
    """
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)

    payload = {
        "type": ACCESS_TOKEN_TYPE,
        "exp": expire,
        "iat": datetime.utcnow(),
        "sub": str(subject),
    }

    if additional_claims:
        payload.update(additional_claims)

    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def create_refresh_token(
        subject: Union[str, Any],
        expires_delta: Optional[timedelta] = None
) -> str:
    """
    Create a new refresh token.

    Args:
        subject: Token subject (usually user ID)
        expires_delta: Token expiration time delta

    Returns:
        JWT refresh token string
    """
    if expires_delta:
        expire = datetime.utcnow() + expires_delta
    else:
        expire = datetime.utcnow() + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)

    payload = {
        "type": REFRESH_TOKEN_TYPE,
        "exp": expire,
        "iat": datetime.utcnow(),
        "sub": str(subject),
    }

    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def decode_access_token(token: str) -> Dict[str, Any]:
    """
    Decode and validate access token.

    Args:
        token: JWT token string

    Returns:
        Token payload

    Raises:
        JWTError: If token is invalid or expired
    """
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])

        if payload.get("type") != ACCESS_TOKEN_TYPE:
            raise JWTError("Invalid token type")

        return payload

    except JWTError as e:
        logger.warning(f"Invalid access token: {e}")
        raise


def decode_refresh_token(token: str) -> Dict[str, Any]:
    """
    Decode and validate refresh token.

    Args:
        token: JWT token string

    Returns:
        Token payload

    Raises:
        JWTError: If token is invalid or expired
    """
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])

        if payload.get("type") != REFRESH_TOKEN_TYPE:
            raise JWTError("Invalid token type")

        return payload

    except JWTError as e:
        logger.warning(f"Invalid refresh token: {e}")
        raise


async def get_current_user(db: AsyncSession, token_payload: Dict[str, Any]) -> Optional[User]:
    """
    Get current user from token payload.

    Args:
        db: Database session
        token_payload: Decoded JWT payload

    Returns:
        User instance or None
    """
    try:
        user_id = token_payload.get("sub")
        if not user_id:
            return None

        user = await user_crud.get_with_roles(db, int(user_id))
        return user

    except (ValueError, TypeError) as e:
        logger.warning(f"Invalid user ID in token: {e}")
        return None


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Verify a password against its hash.

    Args:
        plain_password: Plain text password
        hashed_password: Hashed password

    Returns:
        True if password matches, False otherwise
    """
    return pwd_context.verify(plain_password, hashed_password)


def get_password_hash(password: str) -> str:
    """
    Hash a password.

    Args:
        password: Plain text password

    Returns:
        Hashed password
    """
    return pwd_context.hash(password)


def generate_session_id() -> str:
    """
    Generate a secure session ID.

    Returns:
        Random session ID string
    """
    return secrets.token_urlsafe(32)


def generate_api_key() -> str:
    """
    Generate a secure API key.

    Returns:
        Random API key string
    """
    return secrets.token_urlsafe(48)


def hash_file(file_content: bytes, algorithm: str = "sha256") -> str:
    """
    Calculate hash of file content.

    Args:
        file_content: File content as bytes
        algorithm: Hash algorithm to use

    Returns:
        Hex digest of file hash
    """
    if algorithm == "sha256":
        hasher = hashlib.sha256()
    elif algorithm == "md5":
        hasher = hashlib.md5()
    elif algorithm == "sha1":
        hasher = hashlib.sha1()
    else:
        raise ValueError(f"Unsupported hash algorithm: {algorithm}")

    hasher.update(file_content)
    return hasher.hexdigest()


class PKICertificateValidator:
    """PKI certificate validation and processing."""

    def __init__(self):
        """Initialize certificate validator."""
        self.ca_cert_path = settings.PKI_CA_CERT_PATH
        self.crl_path = settings.PKI_CRL_PATH

    def parse_certificate(self, cert_data: bytes) -> Dict[str, Any]:
        """
        Parse X.509 certificate and extract information.

        Args:
            cert_data: Certificate data in DER or PEM format

        Returns:
            Dictionary with certificate information

        Raises:
            ValueError: If certificate cannot be parsed
        """
        try:
            # Try to load as DER first, then PEM
            try:
                cert = x509.load_der_x509_certificate(cert_data, default_backend())
            except ValueError:
                cert = x509.load_pem_x509_certificate(cert_data, default_backend())

            # Extract certificate information
            subject = cert.subject
            issuer = cert.issuer

            # Get Common Name from subject
            common_name = None
            for attribute in subject:
                if attribute.oid == NameOID.COMMON_NAME:
                    common_name = attribute.value
                    break

            # Get issuer Common Name
            issuer_cn = None
            for attribute in issuer:
                if attribute.oid == NameOID.COMMON_NAME:
                    issuer_cn = attribute.value
                    break

            # Calculate fingerprint
            fingerprint = hashlib.sha256(cert.public_bytes(x509.Encoding.DER)).hexdigest()

            # Get serial number
            serial_number = str(cert.serial_number)

            # Get validity period
            valid_from = cert.not_valid_before
            valid_to = cert.not_valid_after

            # Check if certificate is currently valid
            now = datetime.utcnow()
            is_valid_time = valid_from <= now <= valid_to

            # Extract extended key usage if present
            extended_key_usage = []
            try:
                eku_ext = cert.extensions.get_extension_for_oid(x509.ExtensionOID.EXTENDED_KEY_USAGE)
                for eku in eku_ext.value:
                    if eku == ExtendedKeyUsageOID.CLIENT_AUTH:
                        extended_key_usage.append("client_auth")
                    elif eku == ExtendedKeyUsageOID.SERVER_AUTH:
                        extended_key_usage.append("server_auth")
            except x509.ExtensionNotFound:
                pass

            return {
                "common_name": common_name,
                "serial_number": serial_number,
                "fingerprint": fingerprint,
                "issuer": issuer_cn,
                "subject": subject.rfc4514_string(),
                "valid_from": valid_from,
                "valid_to": valid_to,
                "is_valid_time": is_valid_time,
                "extended_key_usage": extended_key_usage,
                "version": cert.version.name,
                "signature_algorithm": cert.signature_algorithm_oid._name,
            }

        except Exception as e:
            logger.error(f"Failed to parse certificate: {e}")
            raise ValueError(f"Invalid certificate format: {e}")

    def validate_certificate_chain(self, cert_data: bytes) -> bool:
        """
        Validate certificate against CA certificate chain.

        Args:
            cert_data: Certificate data to validate

        Returns:
            True if certificate chain is valid, False otherwise
        """
        if not settings.PKI_CERT_VALIDATION or not self.ca_cert_path:
            logger.warning("Certificate chain validation disabled")
            return True

        try:
            # Load the certificate to validate
            cert = x509.load_der_x509_certificate(cert_data, default_backend())

            # Load CA certificate
            with open(self.ca_cert_path, 'rb') as f:
                ca_cert_data = f.read()

            try:
                ca_cert = x509.load_der_x509_certificate(ca_cert_data, default_backend())
            except ValueError:
                ca_cert = x509.load_pem_x509_certificate(ca_cert_data, default_backend())

            # Verify certificate was signed by CA
            try:
                ca_cert.public_key().verify(
                    cert.signature,
                    cert.tbs_certificate_bytes,
                    cert.signature_algorithm_oid._name
                )
                return True
            except Exception as e:
                logger.warning(f"Certificate chain validation failed: {e}")
                return False

        except Exception as e:
            logger.error(f"Error validating certificate chain: {e}")
            return False

    def check_revocation_status(self, cert_data: bytes) -> bool:
        """
        Check if certificate is revoked using CRL.

        Args:
            cert_data: Certificate data to check

        Returns:
            True if certificate is not revoked, False if revoked
        """
        if not settings.PKI_CERT_VALIDATION or not self.crl_path:
            logger.warning("Certificate revocation checking disabled")
            return True

        try:
            # Load the certificate
            cert = x509.load_der_x509_certificate(cert_data, default_backend())

            # Load CRL
            with open(self.crl_path, 'rb') as f:
                crl_data = f.read()

            try:
                crl = x509.load_der_x509_crl(crl_data, default_backend())
            except ValueError:
                crl = x509.load_pem_x509_crl(crl_data, default_backend())

            # Check if certificate serial number is in CRL
            for revoked_cert in crl:
                if revoked_cert.serial_number == cert.serial_number:
                    logger.warning(f"Certificate {cert.serial_number} is revoked")
                    return False

            return True

        except Exception as e:
            logger.error(f"Error checking certificate revocation: {e}")
            return True  # Assume not revoked if check fails

    def validate_certificate(self, cert_data: bytes) -> Tuple[bool, Dict[str, Any], List[str]]:
        """
        Perform complete certificate validation.

        Args:
            cert_data: Certificate data to validate

        Returns:
            Tuple of (is_valid, cert_info, validation_errors)
        """
        validation_errors = []

        try:
            # Parse certificate
            cert_info = self.parse_certificate(cert_data)

            # Check time validity
            if not cert_info["is_valid_time"]:
                validation_errors.append("Certificate is expired or not yet valid")

            # Check certificate chain
            if not self.validate_certificate_chain(cert_data):
                validation_errors.append("Certificate chain validation failed")

            # Check revocation status
            if not self.check_revocation_status(cert_data):
                validation_errors.append("Certificate is revoked")

            # Check required fields
            if not cert_info["common_name"]:
                validation_errors.append("Certificate missing Common Name")

            # Check extended key usage for client authentication
            if ("client_auth" not in cert_info["extended_key_usage"] and
                    cert_info["extended_key_usage"]):  # Only check if EKU extension exists
                validation_errors.append("Certificate not valid for client authentication")

            is_valid = len(validation_errors) == 0

            if is_valid:
                logger.info(f"Certificate validation successful for CN: {cert_info['common_name']}")
            else:
                logger.warning(f"Certificate validation failed: {validation_errors}")

            return is_valid, cert_info, validation_errors

        except Exception as e:
            validation_errors.append(f"Certificate parsing failed: {str(e)}")
            return False, {}, validation_errors


# Global certificate validator instance
certificate_validator = PKICertificateValidator()


def validate_pki_certificate(cert_data: bytes) -> Tuple[bool, Dict[str, Any], List[str]]:
    """
    Convenience function for certificate validation.

    Args:
        cert_data: Certificate data to validate

    Returns:
        Tuple of (is_valid, cert_info, validation_errors)
    """
    return certificate_validator.validate_certificate(cert_data)
