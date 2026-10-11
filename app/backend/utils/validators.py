"""
Comprehensive input validation and sanitization utilities
Following OWASP security guidelines and industry standards
"""

import re
from app.backend.utils.text import unescape_html, unescape_deep
import bleach
from pydantic import BaseModel, field_validator
from email_validator import validate_email, EmailNotValidError
from app.backend.errors.coded import invalid
from app.backend.enums.errors import FieldErrorCode


class SecurityValidators:
    """Security-focused validators for common input types"""

    # Regex patterns for common validations
    PATTERNS = {
        'alphanumeric': re.compile(r'^[a-zA-Z0-9]+$'),
        'alphanumeric_space': re.compile(r'^[a-zA-Z0-9\s]+$'),
        'username': re.compile(r'^[a-zA-Z0-9_.-]{3,50}$'),
        'filename': re.compile(r'^[a-zA-Z0-9._-]+$'),
        'hex_string': re.compile(r'^[a-fA-F0-9]+$'),
        'uuid': re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'),
        'ip_address': re.compile(r'^(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)$'),
        'mac_address': re.compile(r'^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$'),
        'url_safe': re.compile(r'^[a-zA-Z0-9._~:/?#[\]@!$&\'()*+,;=-]+$'),
        'sql_injection': re.compile(r'(\b(SELECT|INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|EXEC|UNION|SCRIPT)\b)', re.IGNORECASE),
        'xss_patterns': re.compile(r'(<script|javascript:|vbscript:|onload=|onerror=|onclick=)', re.IGNORECASE)
    }

    @classmethod
    def sanitize_string(cls, value: str, max_length: int = 255, allow_html: bool = False) -> str:
        """Validate a plain-text value and return it as the characters the user typed.

        The value is stored and returned as plain text: apostrophes, quotes and
        ampersands stay as they are (the API is JSON, escaping belongs to the
        renderer). Entities a client sends anyway ("&#x27;", "&amp;") are decoded
        first so the checks below see the real text.
        """
        if not isinstance(value, str):
            raise invalid(FieldErrorCode.VALUE_NOT_STRING)

        # Decode entities, then trim whitespace
        value = unescape_html(value).strip()

        # Length validation
        if len(value) > max_length:
            raise invalid(FieldErrorCode.STRING_TOO_LONG, max_length=max_length)

        # Check for SQL injection patterns
        if cls.PATTERNS['sql_injection'].search(value):
            raise invalid(FieldErrorCode.TEXT_SQL_PATTERN)

        # Check for XSS patterns
        if cls.PATTERNS['xss_patterns'].search(value):
            raise invalid(FieldErrorCode.TEXT_SCRIPT_PATTERN)

        if allow_html:
            # Keep only harmless formatting tags
            allowed_tags = ['b', 'i', 'u', 'em', 'strong', 'p', 'br']
            value = bleach.clean(value, tags=allowed_tags, strip=True)

        return value

    @classmethod
    def validate_username(cls, username: str) -> str:
        """Validate username with security rules and keep the case the user typed.

        Uniqueness is case-insensitive (see api/v1/users.py), so "Ivan" and "ivan" can't coexist.
        """
        if not cls.PATTERNS['username'].match(username):
            raise invalid(FieldErrorCode.USERNAME_INVALID)

        # Check for reserved usernames
        reserved = ['admin', 'root', 'user', 'test', 'guest', 'anonymous', 'null', 'undefined']
        if username.lower() in reserved:
            raise invalid(FieldErrorCode.USERNAME_RESERVED)

        return username

    @classmethod
    def validate_email(cls, email: str) -> str:
        """Validate email address"""
        try:
            valid = validate_email(email)
            return valid.email
        except EmailNotValidError:
            raise invalid(FieldErrorCode.EMAIL_INVALID)

    @classmethod
    def validate_ip_address(cls, ip: str) -> str:
        """Validate IPv4 address"""
        if not cls.PATTERNS['ip_address'].match(ip):
            raise invalid(FieldErrorCode.IP_ADDRESS_INVALID)
        return ip

    @classmethod
    def validate_filename(cls, filename: str) -> str:
        """Validate filename for security"""
        if not cls.PATTERNS['filename'].match(filename):
            raise invalid(FieldErrorCode.FILENAME_INVALID)
        
        # Check for path traversal
        if '..' in filename or '/' in filename or '\\' in filename:
            raise invalid(FieldErrorCode.FILENAME_TRAVERSAL)
        
        # Check length
        if len(filename) > 255:
            raise invalid(FieldErrorCode.FILENAME_TOO_LONG)
            
        return filename

    @classmethod
    def validate_integer_range(cls, value: int, min_val: int = None, max_val: int = None) -> int:
        """Validate integer within range"""
        if not isinstance(value, int):
            raise invalid(FieldErrorCode.INTEGER_REQUIRED)
        
        if min_val is not None and value < min_val:
            raise invalid(FieldErrorCode.VALUE_TOO_SMALL, min_value=min_val)
        
        if max_val is not None and value > max_val:
            raise invalid(FieldErrorCode.VALUE_TOO_LARGE, max_value=max_val)
        
        return value

    @classmethod
    def validate_coordinates(cls, lat: float, lon: float) -> tuple[float, float]:
        """Validate geographic coordinates"""
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            raise invalid(FieldErrorCode.COORDINATES_NOT_NUMERIC)
        
        if not (-90 <= lat <= 90):
            raise invalid(FieldErrorCode.LATITUDE_OUT_OF_RANGE)
        
        if not (-180 <= lon <= 180):
            raise invalid(FieldErrorCode.LONGITUDE_OUT_OF_RANGE)
        
        return float(lat), float(lon)


class PlainTextModel(BaseModel):
    """Base for models whose string fields must reach the client as plain text.

    Every string (also inside lists and dicts) is passed through unescape_html
    after validation, so a database row still holding "O&#x27;Brien" from the
    time the API escaped its input is returned as "O'Brien". Nested models clean
    themselves when they inherit this class as well.
    """

    @field_validator("*", mode="after")
    @classmethod
    def _unescape_plain_text(cls, value):
        return unescape_deep(value)


class SecureBaseModel(PlainTextModel):
    """Base model with built-in security validations"""

    model_config = {
        "str_strip_whitespace": True,
        "validate_assignment": True,
        "from_attributes": True,
        "extra": "forbid"  # Reject unknown fields
    }


def _refuse_null(cls, value):
    if value is None:
        raise invalid(FieldErrorCode.VALUE_NULL)
    return value


def not_null(*fields: str):
    """Field validator for an update model: an explicit null for a NOT NULL column answers 422 `value_null`.

    The fields of an update model default to None ("not sent"), and validators do not run on a default, so a None
    that reaches this validator was sent. Use it as a class attribute: `refuse_null = not_null("name", "code")`.
    The check runs before the type and format validators of the field.
    """
    return field_validator(*fields, mode="before")(classmethod(_refuse_null))
