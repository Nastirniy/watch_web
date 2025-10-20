"""
Security utility functions for the Radio Monitoring System
"""

import secrets
import hashlib
import hmac
from typing import Optional, Dict, Any
from passlib.context import CryptContext
from passlib.hash import bcrypt
import base64
import os
from datetime import datetime, timedelta, timezone
import re

# Password hashing context
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def generate_secure_token(length: int = 32) -> str:
    """
    Generate a cryptographically secure random token

    Args:
        length: Length of the token in bytes (default 32)

    Returns:
        URL-safe base64 encoded token string
    """
    return secrets.token_urlsafe(length)


def generate_session_id() -> str:
    """Generate a secure session ID"""
    return generate_secure_token(64)


def generate_api_key() -> str:
    """Generate a secure API key"""
    return f"rms_{generate_secure_token(48)}"


def hash_password(password: str) -> str:
    """
    Hash a password using bcrypt

    Args:
        password: Plain text password

    Returns:
        Hashed password string
    """
    return pwd_context.hash(password)


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Verify a password against its hash

    Args:
        plain_password: Plain text password to verify
        hashed_password: Previously hashed password

    Returns:
        True if password matches, False otherwise
    """
    try:
        return pwd_context.verify(plain_password, hashed_password)
    except Exception:
        return False


def generate_password_hash(password: str, salt: Optional[str] = None) -> Dict[str, str]:
    """
    Generate password hash with salt

    Args:
        password: Plain text password
        salt: Optional salt (generates one if not provided)

    Returns:
        Dictionary with hash and salt
    """
    if salt is None:
        salt = secrets.token_hex(32)

    # Combine password and salt
    salted_password = f"{password}{salt}"

    # Generate hash
    password_hash = hashlib.pbkdf2_hmac(
        'sha256',
        salted_password.encode('utf-8'),
        salt.encode('utf-8'),
        100000  # iterations
    )

    return {
        'hash': base64.b64encode(password_hash).decode('utf-8'),
        'salt': salt
    }


def verify_password_with_salt(password: str, stored_hash: str, salt: str) -> bool:
    """
    Verify password with separate salt

    Args:
        password: Plain text password
        stored_hash: Stored password hash
        salt: Salt used for hashing

    Returns:
        True if password matches, False otherwise
    """
    try:
        # Recreate hash with provided salt
        salted_password = f"{password}{salt}"
        computed_hash = hashlib.pbkdf2_hmac(
            'sha256',
            salted_password.encode('utf-8'),
            salt.encode('utf-8'),
            100000
        )
        computed_hash_b64 = base64.b64encode(computed_hash).decode('utf-8')

        # Secure comparison
        return hmac.compare_digest(stored_hash, computed_hash_b64)
    except Exception:
        return False


def sanitize_input(input_string: str, max_length: int = 1000) -> str:
    """
    Sanitize user input to prevent common attacks

    Args:
        input_string: Input string to sanitize
        max_length: Maximum allowed length

    Returns:
        Sanitized string
    """
    if not isinstance(input_string, str):
        return ""

    # Truncate to max length
    sanitized = input_string[:max_length]

    # Remove null bytes
    sanitized = sanitized.replace('\x00', '')

    # Remove or escape potentially dangerous characters
    # Remove control characters except newline, tab, carriage return
    sanitized = ''.join(char for char in sanitized
                        if ord(char) >= 32 or char in '\n\t\r')

    return sanitized.strip()


def validate_email(email: str) -> bool:
    """
    Validate email format

    Args:
        email: Email address to validate

    Returns:
        True if valid email format, False otherwise
    """
    email_pattern = r'^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$'
    return bool(re.match(email_pattern, email)) and len(email) <= 254


def validate_username(username: str) -> bool:
    """
    Validate username format

    Args:
        username: Username to validate

    Returns:
        True if valid username, False otherwise
    """
    # Username rules: 3-50 chars, alphanumeric + underscore/dash, start with letter
    if not username or len(username) < 3 or len(username) > 50:
        return False

    pattern = r'^[a-zA-Z][a-zA-Z0-9._-]*$'
    return bool(re.match(pattern, username))


def mask_sensitive_data(data: str, mask_char: str = '*', show_chars: int = 4) -> str:
    """
    Mask sensitive data for logging/display

    Args:
        data: Sensitive data to mask
        mask_char: Character to use for masking
        show_chars: Number of characters to show at the end

    Returns:
        Masked string
    """
    if not data or len(data) <= show_chars:
        return mask_char * len(data) if data else ""

    return mask_char * (len(data) - show_chars) + data[-show_chars:]


def generate_csrf_token() -> str:
    """Generate CSRF token"""
    return generate_secure_token(32)


def validate_csrf_token(token: str, expected_token: str) -> bool:
    """
    Validate CSRF token

    Args:
        token: Token to validate
        expected_token: Expected token value

    Returns:
        True if tokens match, False otherwise
    """
    if not token or not expected_token:
        return False

    return hmac.compare_digest(token, expected_token)


def secure_compare(a: str, b: str) -> bool:
    """
    Timing-safe string comparison

    Args:
        a: First string
        b: Second string

    Returns:
        True if strings are equal, False otherwise
    """
    return hmac.compare_digest(a.encode('utf-8'), b.encode('utf-8'))


def generate_otp(length: int = 6) -> str:
    """
    Generate numeric OTP (One-Time Password)

    Args:
        length: Length of OTP (default 6)

    Returns:
        Numeric OTP string
    """
    return ''.join(secrets.choice('0123456789') for _ in range(length))


def hash_file_content(content: bytes) -> str:
    """
    Generate SHA-256 hash of file content

    Args:
        content: File content as bytes

    Returns:
        Hexadecimal hash string
    """
    return hashlib.sha256(content).hexdigest()


def validate_file_hash(content: bytes, expected_hash: str) -> bool:
    """
    Validate file content against expected hash

    Args:
        content: File content as bytes
        expected_hash: Expected SHA-256 hash

    Returns:
        True if hash matches, False otherwise
    """
    actual_hash = hash_file_content(content)
    return secure_compare(actual_hash, expected_hash)


def generate_signed_url_token(data: Dict[str, Any], secret_key: str,
                              expires_in: int = 3600) -> str:
    """
    Generate signed token for URL parameters

    Args:
        data: Data to include in token
        secret_key: Secret key for signing
        expires_in: Expiration time in seconds

    Returns:
        Signed token string
    """
    import json

    # Add expiration timestamp
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).timestamp()
    data['expires_at'] = expires_at

    # Convert to JSON and encode
    json_data = json.dumps(data, sort_keys=True)
    encoded_data = base64.urlsafe_b64encode(json_data.encode()).decode()

    # Generate signature
    signature = hmac.new(
        secret_key.encode(),
        encoded_data.encode(),
        hashlib.sha256
    ).hexdigest()

    return f"{encoded_data}.{signature}"


def verify_signed_url_token(token: str, secret_key: str) -> Optional[Dict[str, Any]]:
    """
    Verify and decode signed URL token

    Args:
        token: Signed token to verify
        secret_key: Secret key for verification

    Returns:
        Decoded data if valid, None if invalid or expired
    """
    import json

    try:
        # Split token
        encoded_data, signature = token.split('.')

        # Verify signature
        expected_signature = hmac.new(
            secret_key.encode(),
            encoded_data.encode(),
            hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(signature, expected_signature):
            return None

        # Decode data
        json_data = base64.urlsafe_b64decode(encoded_data).decode()
        data = json.loads(json_data)

        # Check expiration
        if 'expires_at' in data:
            if datetime.now(timezone.utc).timestamp() > data['expires_at']:
                return None
            del data['expires_at']  # Remove expiration from returned data

        return data

    except Exception:
        return None


def rate_limit_key(identifier: str, action: str, window: str = "hour") -> str:
    """
    Generate rate limiting key

    Args:
        identifier: User/IP identifier
        action: Action being rate limited
        window: Time window (hour, minute, day)

    Returns:
        Rate limiting key string
    """
    timestamp = datetime.now(timezone.utc)

    if window == "minute":
        time_key = timestamp.strftime("%Y%m%d%H%M")
    elif window == "hour":
        time_key = timestamp.strftime("%Y%m%d%H")
    elif window == "day":
        time_key = timestamp.strftime("%Y%m%d")
    else:
        time_key = timestamp.strftime("%Y%m%d%H")

    return f"rate_limit:{identifier}:{action}:{time_key}"


def is_safe_redirect_url(url: str, allowed_hosts: list = None) -> bool:
    """
    Check if redirect URL is safe (prevents open redirects)

    Args:
        url: URL to check
        allowed_hosts: List of allowed hosts

    Returns:
        True if URL is safe for redirect, False otherwise
    """
    if not url:
        return False

    # Relative URLs are generally safe
    if url.startswith('/') and not url.startswith('//'):
        return True

    # Check against allowed hosts if provided
    if allowed_hosts:
        from urllib.parse import urlparse
        try:
            parsed = urlparse(url)
            return parsed.netloc in allowed_hosts
        except Exception:
            return False

    return False
