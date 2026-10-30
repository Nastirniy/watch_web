"""
Comprehensive input validation and sanitization utilities
Following OWASP security guidelines and industry standards
"""

import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Annotated
from app.backend.utils.text import unescape_html, unescape_deep
from app.backend.utils.datetime_utils import as_utc, assume_utc
from pydantic import AfterValidator, BaseModel, WrapSerializer, field_validator
from email_validator import validate_email, EmailNotValidError
from app.backend.errors.coded import invalid
from app.backend.enums.errors import FieldErrorCode

# Largest value of a MySQL INT column; request integers stored in one carry this as `le`
INT_MAX = 2147483647

# The calendar of a datetime: a value whose UTC instant lies outside it (0001-01-01T00:00:00+01:00) is refused
CALENDAR_EARLIEST = "0001-01-01T00:00:00Z"
CALENDAR_LATEST = "9999-12-31T23:59:59Z"

# Observation times in request bodies (observed_at, disappeared_at) lie between these bounds: a value like 9999-12-31
# breaks reports and durations. The latest is now + FUTURE_TOLERANCE (a client clock a little ahead), computed when
# the value is checked. Query filters keep UtcDatetime, rows already stored are never checked.
EARLIEST_OBSERVATION_TIME = datetime(2000, 1, 1, tzinfo=timezone.utc)
FUTURE_TOLERANCE = timedelta(days=1)


def _iso_z(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _latest_observation_time() -> datetime:
    return datetime.now(timezone.utc) + FUTURE_TOLERANCE


def utc_instant(value: datetime) -> datetime:
    """The UTC instant of a request datetime; 422 `time_out_of_range` when it lies outside the calendar.

    `as_utc` raises OverflowError for such a value, and pydantic turns only ValueError into a validation error."""
    try:
        return as_utc(value)
    except OverflowError:
        raise invalid(FieldErrorCode.TIME_OUT_OF_RANGE, earliest=CALENDAR_EARLIEST, latest=CALENDAR_LATEST)


def observation_utc_instant(value: datetime) -> datetime:
    """The UTC instant of an observation time; outside the calendar it answers as outside the observation bounds"""
    try:
        return as_utc(value)
    except OverflowError:
        raise invalid(
            FieldErrorCode.TIME_OUT_OF_RANGE,
            earliest=_iso_z(EARLIEST_OBSERVATION_TIME), latest=_iso_z(_latest_observation_time()),
        )


# A datetime a client sends (request body, query parameter, query model): the UTC instant, aware. A value without
# an offset is UTC. The database holds naive UTC and the drivers drop tzinfo, so every datetime that is written or
# compared in SQL must be the UTC instant: 10:00+03:00 is stored and filtered as 07:00.
UtcDatetime = Annotated[datetime, AfterValidator(utc_instant)]


def within_observation_bounds(value: datetime) -> datetime:
    """The UTC instant of an observation time, 422 `time_out_of_range` outside the bounds"""
    latest = _latest_observation_time()
    if not EARLIEST_OBSERVATION_TIME <= value <= latest:
        raise invalid(FieldErrorCode.TIME_OUT_OF_RANGE, earliest=_iso_z(EARLIEST_OBSERVATION_TIME), latest=_iso_z(latest))
    return value


ObservationTime = Annotated[datetime, AfterValidator(observation_utc_instant), AfterValidator(within_observation_bounds)]


def _with_offset(value: datetime, handler):
    # No return annotation: in JSON mode the handler returns the string, and the schema stays the one of datetime
    return handler(assume_utc(value))


# A datetime of a response model: always written with its offset. A value read from the database (naive, UTC) is
# written as UTC ("Z"); an aware value keeps its offset (observation-stats buckets are in the requested zone).
ResponseDatetime = Annotated[datetime, WrapSerializer(_with_offset)]


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
        'xss_patterns': re.compile(r'(<script|javascript:|vbscript:|onload=|onerror=|onclick=)', re.IGNORECASE)
    }

    # Whitespace controls a multi-line text keeps (line breaks and tabs); every other control character is refused
    MULTILINE_CONTROLS = frozenset("\n\r\t")
    # Refused in single-line text as well: the line and paragraph separators, and the bidi embeddings, overrides,
    # isolates and marks that can disguise a text (the same set as the file-name rule of services/files/serve.py,
    # without the BOM). Other format characters stay: ZWJ / ZWNJ spell words.
    SINGLE_LINE_HIDDEN = frozenset(chr(code) for code in (
        0x2028, 0x2029, *range(0x202A, 0x202F), *range(0x2066, 0x206A), 0x200E, 0x200F, 0x061C))

    @classmethod
    def plain_text(cls, value: str, max_length: int, multiline: bool = False) -> str:
        """Natural plain text, the one rule of every free-text field (names, labels, descriptions, comments, guide
        titles): trimmed, at most max_length characters, without script patterns (xss_patterns: text_script_pattern),
        control characters or lone surrogates (text_control_character). multiline keeps line breaks and tabs;
        single-line text also refuses SINGLE_LINE_HIDDEN. May return "": the caller decides what an empty text means.

        There is no keyword list: the ORM binds every value as a parameter, so "Select the post, then create an
        observation" is harmless at the database layer (the SQL-keyword rule was removed on 2026-10-30). The value is
        stored and returned as plain text: apostrophes, quotes and ampersands stay as they are (the API is JSON,
        escaping belongs to the renderer). Entities a client sends anyway ("&#x27;", "&amp;") are decoded first so the
        checks see the real text: "&#10;" is a line break, "&nbsp;" strips away (a reference to a C0 control such as
        "&#7;" is dropped by html.unescape, not refused).
        """
        if not isinstance(value, str):
            raise invalid(FieldErrorCode.VALUE_NOT_STRING)
        value = unescape_html(value).strip()
        if len(value) > max_length:
            raise invalid(FieldErrorCode.STRING_TOO_LONG, max_length=max_length)
        allowed = cls.MULTILINE_CONTROLS if multiline else frozenset()
        hidden = frozenset() if multiline else cls.SINGLE_LINE_HIDDEN
        if any((unicodedata.category(char) in ("Cc", "Cs") and char not in allowed) or char in hidden
               for char in value):
            raise invalid(FieldErrorCode.TEXT_CONTROL_CHARACTER)
        if cls.PATTERNS['xss_patterns'].search(value):
            raise invalid(FieldErrorCode.TEXT_SCRIPT_PATTERN)
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
