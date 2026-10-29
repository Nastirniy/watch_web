"""The one file response, headers and paths (no FastAPI: api/shared/helpers/media_helper.py stored_file_response makes
the Response from these).

Every file the API hands out (originals of media, user guides, generated reports) carries:
- Content-Disposition with the name twice (RFC 6266 with RFC 8187 encoding): an ASCII fallback in filename="..." for
  old clients and the real name in filename*=UTF-8''<percent-encoded>, which every current browser prefers;
- X-Content-Type-Options: nosniff, so a browser never guesses a type other than the stored one;
- Content-Security-Policy: sandbox; default-src 'none'. A stored file opened as a page (navigated to, or put in a
  frame) runs as a sandboxed document of a unique origin with nothing allowed to load: a Markdown guide or an
  uploaded HTML file with a <script> cannot run it on the site's origin. CSP governs documents only: the same file
  loaded by <video src>, <audio src> or <img src> of the web page is a subresource, not a document, so the policy
  does not apply there and playback and display keep working.

Behind nginx (X-Accel-Redirect) nginx adds the safety headers and Cache-Control itself where it serves the file
(nginx-app.conf /protected_uploads/ and the stream location) and the API leaves them out of its answer
(accel_headers): checked live in nginx:1.27, nginx passes on only a few known headers of an X-Accel answer and drops
any other, so a copy of the API's would never arrive, and a header it does pass would arrive twice.
"""
from __future__ import annotations

import unicodedata
from typing import Dict, Mapping, Optional
from urllib.parse import quote

from app.backend.config import settings
from app.backend.utils.content_type import served_content_type

DISPOSITIONS = frozenset({"attachment", "inline"})
CONTENT_SECURITY_POLICY = "sandbox; default-src 'none'"
NOSNIFF = "nosniff"
# Cache-Control of a stored file; nginx-app.conf sends the same where it serves one
STORED_FILE_CACHE_CONTROL = "private, max-age=3600"
# Headers nginx adds where it serves an X-Accel-Redirect: the API leaves them out of that answer (accel_headers)
NGINX_ADDED_HEADERS = ("Cache-Control", "X-Content-Type-Options", "Content-Security-Policy")
# nginx's static module answers Accept-Ranges itself on an X-Accel answer
_NGINX_OWN_HEADERS = ("Accept-Ranges",)

# RFC 8187 attr-char without ALPHA / DIGIT (quote keeps those and "_.-~" itself)
_ATTR_CHAR_SAFE = "!#$&+-.^_`|~"
# Not in the fallback: a quoted-string would need them escaped (", \), and some clients decode "%" there
# (RFC 6266 appendix D)
_FALLBACK_REPLACED = frozenset('"\\%')
_EMPTY_NAME = "file"


def _printable(filename: str) -> str:
    """The name without control and format characters (CR, LF, NUL, bidirectional overrides)"""
    return "".join(ch for ch in filename if unicodedata.category(ch) not in ("Cc", "Cf"))


def _ascii_fallback(filename: str) -> str:
    """ASCII letters of the name: accents dropped (NFKD), every run of other characters one "_" """
    out, replaced = [], False
    for ch in unicodedata.normalize("NFKD", filename):
        if unicodedata.combining(ch):
            continue
        if " " <= ch <= "~" and ch not in _FALLBACK_REPLACED:
            out.append(ch)
            replaced = False
        elif not replaced:
            out.append("_")
            replaced = True
    return "".join(out)


def content_disposition(disposition: str, filename: str) -> str:
    """'attachment; filename="<ASCII fallback>"; filename*=UTF-8''<name, percent-encoded>' (RFC 6266, RFC 8187).
    Both parts always: for an ASCII name they are the same. ValueError for another disposition"""
    if disposition not in DISPOSITIONS:
        raise ValueError("disposition must be attachment or inline")
    name = _printable(filename).strip() or _EMPTY_NAME
    encoded = quote(name.encode("utf-8"), safe=_ATTR_CHAR_SAFE)
    return f"{disposition}; filename=\"{_ascii_fallback(name)}\"; filename*=UTF-8''{encoded}"


def file_headers(*, content_type: str, filename: str, disposition: str, etag: Optional[str]) -> Dict[str, str]:
    """Headers of every file answer: the served Content-Type (text types with their charset), the name, nosniff,
    the sandbox CSP, and the ETag when the file has one (quoted here)"""
    headers = {
        # in the headers, never media_type=: Starlette would add its own charset to a text type
        "Content-Type": served_content_type(content_type),
        "Content-Disposition": content_disposition(disposition, filename),
        "X-Content-Type-Options": NOSNIFF,
        "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    }
    if etag:
        headers["ETag"] = f'"{etag}"'
    return headers


def accel_redirect_path(relative_path: str) -> str:
    """nginx internal path for a file under the uploads root: {nginx_internal_uploads_prefix}/{relative}"""
    prefix = settings.storage.nginx_internal_uploads_prefix.rstrip("/")
    return f"{prefix}/{relative_path.replace(chr(92), '/').lstrip('/')}"


def accel_headers(headers: Mapping[str, str], relative_path: str) -> Dict[str, str]:
    """The answer that hands a file under the uploads root to nginx: the file's headers without those nginx adds or
    sends itself there, plus the redirect"""
    left_out = {name.lower() for name in NGINX_ADDED_HEADERS + _NGINX_OWN_HEADERS}
    answer = {name: value for name, value in headers.items() if name.lower() not in left_out}
    answer["X-Accel-Redirect"] = accel_redirect_path(relative_path)
    answer["X-Accel-Buffering"] = "yes"
    return answer
