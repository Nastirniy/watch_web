"""Upload file names (non-ASCII stems), the nginx temp path contract and temp-file cleanup."""
import os
import time
from pathlib import Path

import pytest

from app.backend.api.v1 import V1
from app.backend.config import settings
from app.backend.enums.routes import MediaRoutes
from app.backend.models.media import MediaFile
from app.backend.services.media import uploads
from tests.helpers import make_post, make_user, make_dictionary, make_observation
from tests.test_media_uploads import CREATE, COMPLETE, CHUNK, SESSION, _create_body, make_png, storage  # noqa: F401

UPLOAD = V1.path(MediaRoutes.UPLOAD)
NGINX_UPLOAD = V1.path(MediaRoutes.NGINX_UPLOAD)


async def _scene(db, perms=("media.view", "media.create")):
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, perms=list(perms), post_id=post.id)
    observation = await make_observation(db, user, post, ids["signal_type_id"])
    return post, user, observation


@pytest.fixture
def temp_dir(tmp_path, monkeypatch):
    directory = tmp_path / "nginx_uploads"
    directory.mkdir()
    monkeypatch.setattr(settings.storage, "enable_nginx", True)
    monkeypatch.setattr(settings.storage, "nginx_upload_temp_path", str(directory))
    return directory


def _temp_file(directory, name="0000000001", data=None):
    path = directory / name
    path.write_bytes(data if data is not None else make_png())
    return path


def _unlink_refused_for(monkeypatch, names):
    """Path.unlink raises PermissionError for these file names (a file nginx's user owns, a read-only mount)"""
    real_unlink = Path.unlink
    refused = []

    def unlink(self, missing_ok=False):
        if self.name in names:
            refused.append(self.name)
            raise PermissionError(13, "Permission denied", str(self))
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    return refused


def _nginx_post(client, headers, user, observation, path, filename="photo.png", media_type="image"):
    return client.post(
        NGINX_UPLOAD, params={"observation_id": observation.id, "media_type": media_type},
        headers={"X-File-Name": str(path), "X-Original-Filename": filename, "Content-Type": "image/png", **headers(user)},
        content=b"",
    )


# ----------------------------------------------------------------- file names

@pytest.mark.parametrize("name,expected_ext", [("фото.png", ".png"), ("снимок 1.PNG", ".png"), ("photo.png", ".png")])
def test_sanitize_keeps_extension(name, expected_ext, storage):
    from app.backend.services.media.manager import get_media_manager
    safe = get_media_manager().sanitize_filename(name)
    assert safe.lower().endswith(expected_ext)
    if name == "фото.png":
        assert safe.startswith("upload_") and len(safe) == len("upload_xxxxxxxx.png")
    if name == "photo.png":
        assert safe == "photo.png"


@pytest.mark.asyncio
async def test_cyrillic_name_through_resumable_upload(client, db, headers, storage):
    post, user, observation = await _scene(db)
    data = make_png(80, 60)
    created = await client.post(CREATE, json=_create_body(observation.id, data, original_filename="фото.png"), headers=headers(user))
    assert created.status_code == 201, created.text
    upload_id = created.json()["id"]
    patched = await client.patch(SESSION.format(upload_id=upload_id), content=data, headers={**CHUNK, "Upload-Offset": "0", **headers(user)})
    assert patched.status_code == 204, patched.text
    done = await client.post(COMPLETE.format(upload_id=upload_id), headers=headers(user))
    assert done.status_code == 200, done.text
    media = await db.get(MediaFile, done.json()["media_file_id"])
    assert media.file_extension == ".png" and media.original_filename.endswith(".png")
    assert media.original_filename.startswith("upload_")


@pytest.mark.asyncio
async def test_cyrillic_name_through_multipart_upload(client, db, headers, storage):
    post, user, observation = await _scene(db)
    response = await client.post(
        UPLOAD, data={"observation_id": observation.id, "media_type": "image"},
        files={"file": ("фото.png", make_png(), "image/png")}, headers=headers(user),
    )
    assert response.status_code in (200, 201), response.text
    media = await db.get(MediaFile, response.json()["media_file_id"])
    assert media.file_extension == ".png" and media.original_filename.endswith(".png")


# ----------------------------------------------------------------- nginx temp path

@pytest.mark.asyncio
async def test_nginx_upload_refuses_paths_outside_the_temp_dir(client, db, headers, storage, temp_dir, tmp_path):
    post, user, observation = await _scene(db)
    outside = _temp_file(tmp_path, "outside.png")
    traversal = temp_dir / ".." / "outside.png"
    for candidate in (outside, traversal, temp_dir / "missing", temp_dir):
        response = await _nginx_post(client, headers, user, observation, candidate)
        assert response.status_code == 400, (candidate, response.text)
        assert response.json()["code"] == "nginx_file_name_invalid"
    assert outside.exists()


@pytest.mark.asyncio
async def test_nginx_upload_accepts_nested_temp_file_and_moves_it(client, db, headers, storage, temp_dir):
    post, user, observation = await _scene(db)
    nested = temp_dir / "1" / "23"
    nested.mkdir(parents=True)
    path = _temp_file(nested, "0000000123")
    response = await _nginx_post(client, headers, user, observation, path)
    assert response.status_code in (200, 201), response.text
    assert not path.exists()


@pytest.mark.asyncio
async def test_rejected_nginx_upload_removes_the_temp_file(client, db, headers, storage, temp_dir):
    post, user, observation = await _scene(db)
    # content of another type: rejected by the store
    bad = _temp_file(temp_dir, "0000000002", b"MZ" + b"\x00" * 64)
    response = await _nginx_post(client, headers, user, observation, bad)
    assert response.status_code == 415 and response.json()["code"] == "media_content_type_not_accepted"
    assert not bad.exists()

    # declared format refused before the store runs
    wrong = _temp_file(temp_dir, "0000000003")
    response = await _nginx_post(client, headers, user, observation, wrong, filename="scan.bmp")
    assert response.status_code == 415
    assert not wrong.exists()

    # observation of another post
    other_post = await make_post(db)
    ids = await make_dictionary(db)
    other_user = await make_user(db, perms=["media.view", "media.create"], post_id=other_post.id)
    foreign = _temp_file(temp_dir, "0000000004")
    response = await _nginx_post(client, headers, other_user, observation, foreign)
    assert response.status_code == 403
    assert not foreign.exists()

    missing_observation = _temp_file(temp_dir, "0000000005")
    ghost = type("Obs", (), {"id": 999999})()
    response = await _nginx_post(client, headers, user, ghost, missing_observation)
    assert response.status_code == 404
    assert not missing_observation.exists()


@pytest.mark.asyncio
async def test_a_failing_temp_cleanup_never_replaces_the_outcome(client, db, headers, storage, temp_dir, monkeypatch):
    """An OSError of the unlink in the finally is logged; the answer stays the one the upload earned"""
    post, user, observation = await _scene(db)
    stored = _temp_file(temp_dir, "0000000006")
    rejected = _temp_file(temp_dir, "0000000007", b"MZ" + b"\x00" * 64)
    refused = _unlink_refused_for(monkeypatch, {stored.name, rejected.name})

    ok = await _nginx_post(client, headers, user, observation, stored)
    assert ok.status_code in (200, 201), ok.text
    bad = await _nginx_post(client, headers, user, observation, rejected)
    assert bad.status_code == 415, bad.text
    assert bad.json()["code"] == "media_content_type_not_accepted"
    assert refused == [stored.name, rejected.name]
    assert rejected.exists(), "left for sweep-nginx-temp"


# ----------------------------------------------------------------- sweep

def test_sweep_removes_only_old_files(tmp_path):
    old = tmp_path / "old"
    old.write_bytes(b"x")
    nested = tmp_path / "1" / "23"
    nested.mkdir(parents=True)
    old_nested = nested / "old_nested"
    old_nested.write_bytes(b"x")
    fresh = nested / "fresh"
    fresh.write_bytes(b"x")
    long_ago = time.time() - 3 * 3600
    for path in (old, old_nested):
        os.utime(path, (long_ago, long_ago))

    removed = uploads.sweep_nginx_temp(tmp_path, max_age_seconds=3600)
    assert removed.files == 2 and removed.bytes == 2
    assert not old.exists() and not old_nested.exists()
    assert fresh.exists() and nested.is_dir()


def test_sweep_of_a_missing_directory_is_a_no_op(tmp_path):
    assert uploads.sweep_nginx_temp(tmp_path / "nothing", max_age_seconds=1).files == 0


def test_media_tools_sweep_command(tmp_path, monkeypatch, capsys):
    from app.backend.cli import media_tools
    monkeypatch.setattr(settings.storage, "nginx_upload_temp_path", str(tmp_path))
    old = tmp_path / "old"
    old.write_bytes(b"x")
    long_ago = time.time() - 48 * 3600
    os.utime(old, (long_ago, long_ago))
    assert media_tools.main(["sweep-nginx-temp"]) == 0
    assert not old.exists()
    assert "1 file" in capsys.readouterr().out


def test_sweep_skips_and_counts_a_file_it_may_not_delete(tmp_path, monkeypatch, capsys):
    long_ago = time.time() - 48 * 3600
    for name in ("a_old", "b_locked", "c_old"):
        (tmp_path / name).write_bytes(b"xy")
        os.utime(tmp_path / name, (long_ago, long_ago))
    _unlink_refused_for(monkeypatch, {"b_locked"})

    result = uploads.sweep_nginx_temp(tmp_path, max_age_seconds=3600)
    assert (result.files, result.bytes, result.skipped) == (2, 4, 1)
    assert (tmp_path / "b_locked").exists()
    assert not (tmp_path / "a_old").exists() and not (tmp_path / "c_old").exists()

    from app.backend.cli import media_tools
    monkeypatch.setattr(settings.storage, "nginx_upload_temp_path", str(tmp_path))
    assert media_tools.main(["sweep-nginx-temp"]) == 0
    out = capsys.readouterr().out
    assert "removed 0 file(s)" in out and "skipped 1 file(s)" in out


@pytest.mark.asyncio
async def test_same_file_name_in_the_same_second_as_an_upload_in_flight(client, db, headers, storage):
    """An upload's row holds a temporary stored_filename (unique) until its file is moved; the name came from the file
    name and the second, so two uploads of photo.png in one second collided (1062, 500 in the live test of 2026-10-19).
    Rows with the names the old scheme gave this second and the next stand for the upload in flight."""
    import hashlib
    from datetime import datetime, timedelta, timezone
    post, user, observation = await _scene(db)
    now = datetime.now(timezone.utc)
    for moment in (now, now + timedelta(seconds=1)):
        name = f"temp_{hashlib.md5(f'photo.png_{moment:%Y%m%d_%H%M%S}'.encode()).hexdigest()[:16]}.png"
        db.add(MediaFile(observation_id=observation.id, user_id=user.id, original_filename="photo.png",
                         stored_filename=name, file_path=f"in-flight/{name}", file_size=1, mime_type="image/png",
                         file_extension=".png", media_type="image", file_hash=name))
    await db.commit()
    response = await client.post(
        UPLOAD, data={"observation_id": observation.id, "media_type": "image"},
        files={"file": ("photo.png", make_png(), "image/png")}, headers=headers(user),
    )
    assert response.status_code in (200, 201), response.text


@pytest.mark.asyncio
async def test_nginx_upload_audit_row_names_the_transport(client, db, headers, storage, temp_dir):
    from sqlmodel import select
    from app.backend.models.audit import AuditLog
    post, user, observation = await _scene(db)
    response = await _nginx_post(client, headers, user, observation, _temp_file(temp_dir, "0000000042"))
    assert response.status_code in (200, 201), response.text
    row = (await db.exec(select(AuditLog).where(AuditLog.event == "media.create"))).one()
    assert row.scope_key == f"p:{observation.post_id}"
    assert row.params == {"observation_id": observation.id, "file_name": "photo.png", "media_type": "image",
                          "via": "nginx_offload"}


# ----------------------------------------------------------------- names a client cannot spell in UTF-8

BOUNDARY = b"filename-probe"
# The UTF-8 bytes of a lone surrogate (invalid UTF-8), stray high bytes, a name outside the BMP, a bidi override with a
# zero width joiner, and a name far over the 255 characters of a file name: a file name, never a 500
RAW_NAMES = {
    "surrogate-bytes": (b"a\xed\xb2\x80.png", ".png"),
    "invalid-utf8": (b"a\x80\xff.png", ".png"),
    "non-bmp": ("radio \U0001f4fb.png".encode("utf-8"), ".png"),
    "format-chars": ("‮abc‍.png".encode("utf-8"), ".png"),
    "very-long": (b"x" * 5000 + b".png", ".png"),
}
MULTIPART = {"Content-Type": "multipart/form-data; boundary=" + BOUNDARY.decode()}


def _raw_multipart(fields: dict, filename: bytes, data: bytes, content_type: bytes) -> bytes:
    """A multipart body written by hand: httpx cannot send a file name that is not valid UTF-8"""
    parts = [b"--" + BOUNDARY + b'\r\nContent-Disposition: form-data; name="' + name.encode() + b'"\r\n\r\n'
             + str(value).encode() + b"\r\n" for name, value in fields.items()]
    parts.append(b"--" + BOUNDARY + b'\r\nContent-Disposition: form-data; name="file"; filename="' + filename
                 + b'"\r\nContent-Type: ' + content_type + b"\r\n\r\n" + data + b"\r\n")
    return b"".join(parts) + b"--" + BOUNDARY + b"--\r\n"


@pytest.mark.parametrize("name", [
    "a\udc80.png", "\udcff\ud800.png", "radio \U0001f4fb.png", "‮abc‍⁦.png", "x" * 5000 + ".png",
    "İstanbul.PNG", "​‌‍.png",
], ids=["lone-surrogate", "only-surrogates", "non-bmp", "format-chars", "very-long", "dotted-capital-i",
        "only-format-chars"])
def test_the_sanitiser_never_fails_and_keeps_the_extension(name):
    """Whatever reaches Python (a surrogate from a decoder with surrogateescape included): an ASCII name within the
    limit, its extension kept, a generated stem when nothing of the stem is left"""
    from app.backend.services.files.names import sanitize_filename
    safe = sanitize_filename(name)
    safe.encode("ascii")
    assert safe.lower().endswith(".png") and 4 < len(safe) <= 200
    assert not any(ch in safe for ch in '<>:"/\\|?* ')


@pytest.mark.asyncio
@pytest.mark.parametrize("key", RAW_NAMES)
async def test_a_raw_file_name_through_the_multipart_upload(client, db, headers, storage, key):
    """Starlette decodes a part's file name as UTF-8 and falls back to Latin-1: no surrogate reaches the store, every
    name lands as the sanitiser leaves it"""
    from app.backend.services.files.names import sanitize_filename
    post, user, observation = await _scene(db)
    filename, extension = RAW_NAMES[key]
    body = _raw_multipart({"observation_id": observation.id, "media_type": "image"}, filename, make_png(), b"image/png")
    response = await client.post(UPLOAD, content=body, headers={**MULTIPART, **headers(user)})
    assert response.status_code == 200, response.text
    media = await db.get(MediaFile, response.json()["media_file_id"])
    assert media.original_filename.endswith(extension) and len(media.original_filename) <= 200
    if key in ("non-bmp", "format-chars"):
        assert media.original_filename == sanitize_filename(filename.decode("utf-8"))


@pytest.mark.asyncio
async def test_a_lone_surrogate_in_a_resumable_upload_is_422(client, db, headers, storage):
    """JSON may escape a lone surrogate: pydantic refuses it as string_unicode before anything is stored"""
    import json
    post, user, observation = await _scene(db)
    body = json.dumps(_create_body(observation.id, make_png(), original_filename="PLACEHOLDER")).encode()
    for raw in (b'"a\\udc80.png"', b'"a\xed\xb2\x80.png"'):
        response = await client.post(CREATE, content=body.replace(b'"PLACEHOLDER"', raw),
                                     headers={"Content-Type": "application/json", **headers(user)})
        assert response.status_code == 422, response.text
        assert [e["code"] for e in response.json()["errors"]] == ["string_unicode"]


@pytest.mark.asyncio
@pytest.mark.parametrize("key", RAW_NAMES)
async def test_a_raw_file_name_through_the_nginx_upload(client, db, headers, storage, temp_dir, key):
    """X-Original-Filename carries the name's UTF-8 bytes (nginx passes the client's header on as it is): read as
    UTF-8, so a name lands as it does through the multipart upload; bytes that are no UTF-8 are read as Latin-1"""
    from app.backend.services.files.names import sanitize_filename
    post, user, observation = await _scene(db)
    filename, extension = RAW_NAMES[key]
    response = await _nginx_post(client, headers, user, observation, _temp_file(temp_dir), filename=filename)
    assert response.status_code == 200, response.text
    media = await db.get(MediaFile, response.json()["media_file_id"])
    assert media.original_filename.endswith(extension) and len(media.original_filename) <= 200
    try:
        expected = filename.decode("utf-8")
    except UnicodeDecodeError:
        expected = filename.decode("latin-1")
    assert media.original_filename == sanitize_filename(expected)
