"""Plain-text normalisation for values that travel through the JSON API.

Earlier versions HTML-escaped every incoming string (html.escape), so the
database holds values such as ``O&#x27;Brien`` instead of ``O'Brien``. The API
speaks JSON, not HTML, so escaping is the job of whoever renders the text.
These helpers turn stored or incoming entity sequences back into characters.
"""
import html
import re
from typing import Any, Optional

# Entity written without the leading '#', e.g. "&x27;" or "&39;": not valid
# HTML, but it is what a stripped "&#x27;" looks like after some clients touch it.
_BARE_NUMERIC_ENTITY = re.compile(r"&(x[0-9a-fA-F]{1,6}|[0-9]{1,7});")
_ANY_ENTITY = re.compile(r"&(#?[0-9a-zA-Z]{1,32});")

# One html.escape adds one layer. A value that was saved, read back escaped and
# saved again carries two ("&amp;#x27;"). Three layers is more than we ever produced.
MAX_UNESCAPE_ROUNDS = 3


def _unescape_bare_numeric(match: "re.Match[str]") -> str:
    body = match.group(1)
    try:
        code = int(body[1:], 16) if body[0] in "xX" else int(body)
        return chr(code)
    except (ValueError, OverflowError):
        return match.group(0)


def unescape_html(value: str) -> str:
    """Return ``value`` with HTML entities replaced by the characters they encode.

    Works for named (``&amp;``), decimal (``&#39;``), hexadecimal (``&#x27;``) and
    the malformed bare form (``&x27;``). Repeats while the text still shrinks so a
    double-escaped value comes out clean too. Text without entities is returned as is.
    """
    if not isinstance(value, str) or "&" not in value:
        return value
    current = value
    for _ in range(MAX_UNESCAPE_ROUNDS):
        if not _ANY_ENTITY.search(current):
            break
        candidate = html.unescape(current)
        candidate = _BARE_NUMERIC_ENTITY.sub(_unescape_bare_numeric, candidate)
        if candidate == current:
            break
        current = candidate
    return current


def unescape_deep(value: Any) -> Any:
    """Apply unescape_html to every string inside plain containers (str, list, tuple, dict).

    Other objects (numbers, datetimes, pydantic models) are returned untouched.
    """
    if isinstance(value, str):
        return unescape_html(value)
    if isinstance(value, list):
        return [unescape_deep(item) for item in value]
    if isinstance(value, tuple):
        return tuple(unescape_deep(item) for item in value)
    if isinstance(value, dict):
        return {key: unescape_deep(item) for key, item in value.items()}
    return value


def fit_column(column: Any, value: Optional[str]) -> Optional[str]:
    """A request-derived string cut to the length of its column (MySQL refuses a longer one in strict mode).
    column is a model attribute such as AuditLog.request_url; a column without a length keeps the value."""
    length = getattr(column.type, "length", None)
    if value is None or length is None:
        return value
    return value[:length]
