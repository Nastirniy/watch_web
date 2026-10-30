"""The file's own ETag and Repr-Digest (RFC 9530) on every stored-file answer, and If-None-Match answered by the API.

A stored file carries ETag: "<sha256 hex>" and Repr-Digest: sha-256=:<base64 of the digest>:, in an X-Accel answer
as well (nginx passes them on, nginx-app.conf). If-None-Match is answered by the API before it hands the file to
nginx: nginx cannot give a 304 for an ETag of the API (its not_modified filter runs before add_header). The access
checks come first: a caller without access never sees the ETag."""
import base64
import hashlib

import pytest

from app.backend.api.v1 import V1
from app.backend.config import settings
from app.backend.enums.media_types import ContentType
from app.backend.enums.routes import GuideRoutes, MediaRoutes
from app.backend.services.files.serve import (
    NGINX_ADDED_HEADERS, accel_headers, file_headers, none_match, repr_digest,
)
from tests.helpers import make_admin, make_dictionary, make_media, make_observation, make_post, make_user

ORIGINAL_ROUTES = (MediaRoutes.MEDIA_DOWNLOAD, MediaRoutes.MEDIA_STREAM)
CONTENT = b"original bytes of the file"
SHA256 = hashlib.sha256(CONTENT).hexdigest()
DIGEST = "sha-256=:" + base64.b64encode(hashlib.sha256(CONTENT).digest()).decode() + ":"
ETAG = f'"{SHA256}"'
OTHER = '"' + "0" * 64 + '"'


# ------------------------------------------------------------------ the header values
def test_repr_digest_is_the_base64_of_the_raw_digest():
    assert repr_digest(hashlib.sha256(b"abc").hexdigest()) == "sha-256=:ungWv48Bz+pBQUDeXa4iI7ADYaOWF3qctBD/YfIAFa0=:"


@pytest.mark.parametrize("value", [None, "", "abc", "z" * 64, "0" * 63, "0" * 65])
def test_a_value_that_is_no_sha256_gives_no_digest(value):
    assert repr_digest(value) is None


def test_file_headers_carry_the_etag_and_the_digest():
    headers = file_headers(content_type=ContentType.PDF.value, filename="a.pdf", disposition="inline", etag=SHA256)
    assert headers["ETag"] == ETAG and headers["Repr-Digest"] == DIGEST
    bare = file_headers(content_type=ContentType.PDF.value, filename="a.pdf", disposition="inline", etag=None)
    assert "ETag" not in bare and "Repr-Digest" not in bare


def test_an_x_accel_answer_keeps_the_etag_and_the_digest():
    """nginx passes them on from the upstream answer (add_header ETag $upstream_http_etag, nginx-app.conf)"""
    answer = accel_headers(file_headers(content_type=ContentType.PDF.value, filename="a.pdf", disposition="inline",
                                        etag=SHA256), "a/b.pdf")
    assert answer["ETag"] == ETAG and answer["Repr-Digest"] == DIGEST
    for name in NGINX_ADDED_HEADERS:
        assert name not in answer


@pytest.mark.parametrize("header,matches", [
    (None, False),
    ("", False),
    ("*", True),
    (" * ", True),
    (ETAG, True),
    (f"W/{ETAG}", True),                       # weak comparison, RFC 9110 section 13.1.2
    (f'{OTHER}, {ETAG}', True),
    (f'{OTHER},W/{ETAG}  ', True),
    (f'W/{OTHER} ,  {ETAG}', True),
    (OTHER, False),
    (SHA256, False),                           # no quotes: no entity tag
    (f'"{SHA256}', False),
    (f'{OTHER} {ETAG}', False),                # no comma: not a list
    (f'w/{ETAG}', False),                      # W/ is case sensitive
    (f'"{SHA256.upper()}"', False),            # opaque tags compare byte by byte
    (f'garbage, {ETAG}', False),               # what cannot be read is no match: the file is served
    ('"*"', False),
])
def test_none_match(header, matches):
    assert none_match(header, SHA256) is matches


def test_a_file_without_an_etag_matches_only_a_star():
    assert none_match("*", None) is True
    assert none_match('""', None) is False
    assert none_match(ETAG, None) is False


# ------------------------------------------------------------------ the routes
@pytest.fixture
def storage(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.storage, "upload_path", str(tmp_path))
    monkeypatch.setattr(settings.storage, "temp_path", str(tmp_path / "spool"))
    monkeypatch.setattr(settings.storage, "enable_nginx", False)
    return tmp_path


async def _media(db, storage, on_disk=True):
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, perms=["media.view"], post_id=post.id)
    observation = await make_observation(db, user, post, ids["signal_type_id"])
    media = await make_media(db, observation, user, file_hash=SHA256)
    if on_disk:
        (storage / media.file_path).parent.mkdir(parents=True)
        (storage / media.file_path).write_bytes(CONTENT)
    return user, media


@pytest.mark.asyncio
@pytest.mark.parametrize("enable_nginx", [True, False])
@pytest.mark.parametrize("route", ORIGINAL_ROUTES)
async def test_an_original_carries_its_etag_and_digest(client, db, headers, storage, monkeypatch, route, enable_nginx):
    monkeypatch.setattr(settings.storage, "enable_nginx", enable_nginx)
    user, media = await _media(db, storage)
    response = await client.get(V1.path(route, media_id=media.id), headers=headers(user))
    assert response.status_code == 200, response.text
    assert response.headers["etag"] == ETAG and response.headers["repr-digest"] == DIGEST


@pytest.mark.asyncio
async def test_a_part_of_the_file_carries_the_digest_of_the_whole_file(client, db, headers, storage):
    user, media = await _media(db, storage)
    response = await client.get(V1.path(MediaRoutes.MEDIA_DOWNLOAD, media_id=media.id),
                                headers={**headers(user), "Range": "bytes=0-3"})
    assert response.status_code == 206 and response.content == CONTENT[:4]
    assert response.headers["etag"] == ETAG and response.headers["repr-digest"] == DIGEST


@pytest.mark.asyncio
@pytest.mark.parametrize("enable_nginx", [True, False])
@pytest.mark.parametrize("route", ORIGINAL_ROUTES)
@pytest.mark.parametrize("value", [ETAG, f"W/{ETAG}", f"{OTHER}, {ETAG}", "*"])
async def test_if_none_match_answers_304_before_the_file_is_handed_over(client, db, headers, storage, monkeypatch,
                                                                        route, enable_nginx, value):
    monkeypatch.setattr(settings.storage, "enable_nginx", enable_nginx)
    user, media = await _media(db, storage)
    response = await client.get(V1.path(route, media_id=media.id), headers={**headers(user), "If-None-Match": value})
    assert response.status_code == 304, response.text
    assert response.content == b""
    assert response.headers["etag"] == ETAG
    assert response.headers["cache-control"] == "private, max-age=3600", "a 304 sends what a 200 would (RFC 9110 15.4.5)"
    assert "x-accel-redirect" not in response.headers, "the file is not handed to nginx"
    assert "content-length" not in response.headers or response.headers["content-length"] == "0"


@pytest.mark.asyncio
@pytest.mark.parametrize("enable_nginx", [True, False])
async def test_if_none_match_of_another_version_serves_the_file(client, db, headers, storage, monkeypatch, enable_nginx):
    monkeypatch.setattr(settings.storage, "enable_nginx", enable_nginx)
    user, media = await _media(db, storage)
    response = await client.get(V1.path(MediaRoutes.MEDIA_DOWNLOAD, media_id=media.id),
                                headers={**headers(user), "If-None-Match": OTHER})
    assert response.status_code == 200, response.text
    assert ("x-accel-redirect" in response.headers) is enable_nginx
    assert response.headers["etag"] == ETAG


@pytest.mark.asyncio
async def test_if_none_match_comes_before_range(client, db, headers, storage):
    """RFC 9110 section 13.2.2: a matching If-None-Match answers 304 whatever the Range"""
    user, media = await _media(db, storage)
    response = await client.get(V1.path(MediaRoutes.MEDIA_DOWNLOAD, media_id=media.id),
                                headers={**headers(user), "If-None-Match": ETAG, "Range": "bytes=999999-"})
    assert response.status_code == 304


@pytest.mark.asyncio
async def test_repeated_if_none_match_lines_are_one_list(client, db, headers, storage):
    user, media = await _media(db, storage)
    response = await client.get(V1.path(MediaRoutes.MEDIA_DOWNLOAD, media_id=media.id),
                                headers=[*headers(user).items(), ("If-None-Match", OTHER), ("If-None-Match", ETAG)])
    assert response.status_code == 304


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ORIGINAL_ROUTES)
async def test_a_caller_without_access_never_learns_the_etag(client, db, headers, storage, route):
    user, media = await _media(db, storage)
    other_post = await make_post(db)
    stranger = await make_user(db, perms=["media.view"], post_id=other_post.id)
    path = V1.path(route, media_id=media.id)
    for value in (ETAG, "*"):
        refused = await client.get(path, headers={**headers(stranger), "If-None-Match": value})
        assert refused.status_code == 403, refused.text
        assert "etag" not in refused.headers and "repr-digest" not in refused.headers
        anonymous = await client.get(path, headers={"If-None-Match": value})
        assert anonymous.status_code == 401 and "etag" not in anonymous.headers


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [ETAG, "*"])
async def test_a_file_gone_from_disk_is_never_not_modified(client, db, headers, storage, value):
    user, media = await _media(db, storage, on_disk=False)
    response = await client.get(V1.path(MediaRoutes.MEDIA_DOWNLOAD, media_id=media.id),
                                headers={**headers(user), "If-None-Match": value})
    assert response.status_code == 404 and response.json()["code"] == "media_file_missing"


# ------------------------------------------------------------------ guide files
PDF = b"%PDF-1.4 a guide\n%%EOF\n"


async def _guide_file(client, db, headers, **body):
    admin = await make_admin(db)
    guide = await client.post(V1.path(GuideRoutes.ROOT), headers=headers(admin), json={"title": "Guide", **body})
    assert guide.status_code == 201, guide.text
    uploaded = await client.post(V1.path(GuideRoutes.FILES, guide_id=guide.json()["id"]), headers=headers(admin),
                                 files={"file": ("guide.pdf", PDF, "application/pdf")})
    assert uploaded.status_code == 201, uploaded.text
    return admin, guide.json()["id"], uploaded.json()


@pytest.mark.asyncio
@pytest.mark.parametrize("enable_nginx", [True, False])
async def test_a_guide_file_carries_its_etag_and_answers_if_none_match(client, db, headers, storage, monkeypatch,
                                                                       enable_nginx):
    admin, guide_id, body = await _guide_file(client, db, headers)
    monkeypatch.setattr(settings.storage, "enable_nginx", enable_nginx)
    path = V1.path(GuideRoutes.FILE_DOWNLOAD, guide_id=guide_id, file_id=body["id"])
    etag = f'"{body["sha256"]}"'
    digest = "sha-256=:" + base64.b64encode(bytes.fromhex(body["sha256"])).decode() + ":"

    response = await client.get(path, headers=headers(admin))
    assert response.status_code == 200 and response.headers["etag"] == etag
    assert response.headers["repr-digest"] == digest

    response = await client.get(path, headers={**headers(admin), "If-None-Match": etag})
    assert response.status_code == 304 and response.content == b"" and response.headers["etag"] == etag
    assert "x-accel-redirect" not in response.headers


@pytest.mark.asyncio
async def test_a_guide_the_caller_does_not_see_never_answers_304(client, db, headers, storage):
    """Audience first: a guide the caller does not see is 404, whatever the ETag it sends"""
    post = await make_post(db)
    admin, guide_id, body = await _guide_file(client, db, headers, audience={"kind": "post", "id": post.id})
    other_post = await make_post(db)
    outsider = await make_user(db, post_id=other_post.id)
    response = await client.get(V1.path(GuideRoutes.FILE_DOWNLOAD, guide_id=guide_id, file_id=body["id"]),
                                headers={**headers(outsider), "If-None-Match": f'"{body["sha256"]}"'})
    assert response.status_code == 404 and response.json()["code"] == "guide_not_found", response.text
    assert "etag" not in response.headers
