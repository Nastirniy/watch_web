"""One file response for every stored file (services/files/serve.py, api/shared/helpers/media_helper.py).

Content-Disposition names follow RFC 6266 with RFC 8187 encoding: an ASCII fallback in filename="..." plus the real
name in filename*=UTF-8''<percent-encoded>. Every file response carries X-Content-Type-Options: nosniff and the
sandbox Content-Security-Policy, so a stored file (a Markdown guide with a <script>, an uploaded HTML page) never
runs as a page of the site.

Behind nginx (X-Accel-Redirect) nginx adds both headers in the location that serves the file, and the API leaves
them out of its X-Accel answer: nginx does not pass an unknown upstream header on to the client after an X-Accel
redirect (checked live in nginx:1.27, the comment at /protected_uploads/ in nginx-app.conf), and two copies of a
header must never reach the client.
"""
import re
from urllib.parse import unquote

import pytest

from app.backend.api.v1 import V1
from app.backend.config import settings
from app.backend.enums.media_types import ContentType
from app.backend.enums.routes import MediaRoutes, ObservationRoutes
from app.backend.services.files.serve import (CONTENT_SECURITY_POLICY, content_disposition, file_headers,
                                              NGINX_ADDED_HEADERS)
from tests.helpers import make_admin, make_dictionary, make_media, make_observation, make_post, make_user
from tests.test_nginx_conf import APP, _location

CSP = "sandbox; default-src 'none'"
# "posibnyk" in Ukrainian, written with escapes (ASCII-only sources)
CYRILLIC = "\u043f\u043e\u0441\u0456\u0431\u043d\u0438\u043a"
_FALLBACK = re.compile(r'filename="([^"\\]*)"')
_STAR = re.compile(r"filename\*=UTF-8''([A-Za-z0-9!#$&+\-.^_`|~%]*)")


def _names(header: str) -> tuple[str, str]:
    """(ASCII fallback, decoded filename*) of a Content-Disposition value"""
    fallback = _FALLBACK.search(header)
    star = _STAR.search(header)
    assert fallback and star, header
    return fallback.group(1), unquote(star.group(1), encoding="utf-8", errors="strict")


# ------------------------------------------------------------------ names (RFC 6266 / 8187)

def test_cyrillic_name_has_filename_star_and_ascii_fallback():
    name = f"{CYRILLIC} 2026.md"
    header = content_disposition("attachment", name)
    header.encode("ascii")                                   # a header value is ASCII
    assert header.startswith("attachment; ")
    fallback, decoded = _names(header)
    assert decoded == name                                   # the real name round-trips
    assert fallback.isascii() and fallback.endswith(" 2026.md") and fallback != name
    assert "%D0%BF" in header                                 # UTF-8 percent-encoded (RFC 8187)


def test_accented_latin_falls_back_to_its_letters():
    fallback, decoded = _names(content_disposition("inline", "caf\u00e9 r\u00e9sum\u00e9.pdf"))
    assert fallback == "cafe resume.pdf"
    assert decoded == "caf\u00e9 r\u00e9sum\u00e9.pdf"


def test_ascii_name_is_the_same_in_both():
    header = content_disposition("attachment", "report_2026.xlsx")
    assert header == "attachment; filename=\"report_2026.xlsx\"; filename*=UTF-8''report_2026.xlsx"


def test_quotes_and_backslashes_in_names_are_safe():
    name = 'a"b\\c;d%e\r\nX-Evil: 1\x00.txt'
    header = content_disposition("attachment", name)
    header.encode("ascii")
    assert "\r" not in header and "\n" not in header and "\x00" not in header
    fallback, decoded = _names(header)
    # the fallback is one quoted-string: no quote, no backslash, no percent sign (RFC 6266 appendix D) inside
    assert not set(fallback) & set('"\\%') and fallback.endswith(".txt")
    # filename* keeps every printable character; control characters are dropped from both
    assert decoded == 'a"b\\c;d%eX-Evil: 1.txt'


def test_an_empty_name_still_has_a_name():
    fallback, decoded = _names(content_disposition("attachment", "\r\n"))
    assert fallback and decoded == fallback


def test_only_attachment_or_inline():
    with pytest.raises(ValueError):
        content_disposition("form-data", "a.txt")


def test_file_headers_carry_the_type_the_name_and_the_safety_headers():
    headers = file_headers(content_type=ContentType.TEXT_MARKDOWN.value, filename=f"{CYRILLIC}.md",
                           disposition="attachment", etag="abc")
    assert headers["Content-Type"] == "text/markdown; charset=UTF-8"
    assert _names(headers["Content-Disposition"])[1] == f"{CYRILLIC}.md"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Content-Security-Policy"] == CSP == CONTENT_SECURITY_POLICY
    assert headers["ETag"] == '"abc"'
    assert "ETag" not in file_headers(content_type=ContentType.PDF.value, filename="a.pdf", disposition="inline",
                                      etag=None)


# ------------------------------------------------------------------ every file response

@pytest.fixture
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.storage, "upload_path", str(tmp_path))
    return tmp_path


SCRIPT = b"# Guide\n\n<script>alert(1)</script>\n"


async def _markdown_media(db, storage):
    """A Markdown original with a <script> in it, under a Cyrillic name"""
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, perms=["media.view"], post_id=post.id)
    observation = await make_observation(db, user, post, ids["signal_type_id"])
    media = await make_media(db, observation, user, original_filename=f"{CYRILLIC}.md",
                             stored_filename="stored.md", file_path=f"2026/10/obs_{observation.id}/stored.md",
                             mime_type=ContentType.TEXT_MARKDOWN.value, file_extension="md", file_size=len(SCRIPT))
    (storage / media.file_path).parent.mkdir(parents=True)
    (storage / media.file_path).write_bytes(SCRIPT)
    return user, media


def _added_once(location: str, header: str) -> str:
    """The value of the one add_header of `header` in an nginx-app.conf location"""
    found = re.findall(rf"add_header {re.escape(header)} (\"[^\"]*\"|\S+)( always)?;", _location(APP, location))
    assert len(found) == 1, (location, header, found)
    return found[0][0].strip('"')


PROTECTED = "^~ /protected_uploads/"
STREAM = r"~ ^/api/v1/media/\d+/stream$"


def _assert_safe(response, *, behind_nginx: bool, nginx_locations=()):
    if not behind_nginx:
        assert response.headers.get_list("x-content-type-options") == ["nosniff"]
        assert response.headers.get_list("content-security-policy") == [CSP]
        return
    # Behind nginx the API answers only the X-Accel redirect: nginx adds both headers where it serves the file,
    # each once, and the API's answer holds none of the headers nginx adds there (a second copy)
    assert response.headers["x-accel-redirect"]
    for name in NGINX_ADDED_HEADERS:
        assert name.lower() not in response.headers, name
    assert "accept-ranges" not in response.headers       # nginx's static module sends its own
    for location in nginx_locations:
        assert _added_once(location, "X-Content-Type-Options") == "nosniff"
        assert _added_once(location, "Content-Security-Policy") == CSP


@pytest.mark.asyncio
@pytest.mark.parametrize("enable_nginx", [False, True])
@pytest.mark.parametrize("route, disposition, locations", [
    (MediaRoutes.MEDIA_DOWNLOAD, "attachment", (PROTECTED,)),
    (MediaRoutes.MEDIA_STREAM, "inline", (PROTECTED, STREAM)),   # nginx serves a stream in either location
])
async def test_every_file_response_has_nosniff_and_csp(client, db, headers, storage, monkeypatch, enable_nginx,
                                                       route, disposition, locations):
    monkeypatch.setattr(settings.storage, "enable_nginx", enable_nginx)
    user, media = await _markdown_media(db, storage)
    response = await client.get(V1.path(route, media_id=media.id), headers=headers(user))
    assert response.status_code == 200
    assert response.headers["content-disposition"].startswith(f"{disposition}; ")
    assert _names(response.headers["content-disposition"])[1] == f"{CYRILLIC}.md"
    assert response.headers["content-type"] == "text/markdown; charset=UTF-8"
    _assert_safe(response, behind_nginx=enable_nginx, nginx_locations=locations)
    if not enable_nginx:
        assert response.content == SCRIPT
        assert response.headers.get_list("cache-control") == ["private, max-age=3600"]
        assert response.headers.get_list("accept-ranges") == ["bytes"]


@pytest.mark.asyncio
@pytest.mark.parametrize("route", [MediaRoutes.MEDIA_DOWNLOAD, MediaRoutes.MEDIA_STREAM])
async def test_a_range_answer_keeps_the_safety_headers(client, db, headers, storage, monkeypatch, route):
    monkeypatch.setattr(settings.storage, "enable_nginx", False)
    user, media = await _markdown_media(db, storage)
    response = await client.get(V1.path(route, media_id=media.id), headers={**headers(user), "Range": "bytes=0-6"})
    assert response.status_code == 206
    assert response.content == SCRIPT[:7]
    _assert_safe(response, behind_nginx=False)


@pytest.mark.asyncio
@pytest.mark.parametrize("enable_nginx", [False, True])
async def test_the_excel_report_has_nosniff_and_csp(client, db, headers, monkeypatch, enable_nginx):
    """The report is built in memory and never goes through nginx's file locations: the API sends both headers"""
    monkeypatch.setattr(settings.storage, "enable_nginx", enable_nginx)
    response = await client.post(V1.path(ObservationRoutes.REPORT), json={"report_type": "all_fields"},
                                 headers=headers(await make_admin(db)))
    assert response.status_code == 200, response.text
    assert response.headers["content-type"] == ContentType.XLSX.value
    fallback, decoded = _names(response.headers["content-disposition"])
    assert response.headers["content-disposition"].startswith("attachment; ")
    assert fallback == decoded and decoded.startswith("observations_report_") and decoded.endswith(".xlsx")
    _assert_safe(response, behind_nginx=False)


def test_the_headers_nginx_adds_on_x_accel_answers_are_added_by_the_location():
    """What the API leaves out of an X-Accel answer, the location adds (each once)"""
    for name in NGINX_ADDED_HEADERS:
        _added_once(PROTECTED, name)
        _added_once(STREAM, name)
