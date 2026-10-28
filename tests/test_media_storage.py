"""Media storage: the move into storage and the storage statistics keep their filesystem work off the event loop,
and the statistics count in SQL instead of loading every media row."""
import os
import shutil
import threading
from pathlib import Path

import pytest
from sqlalchemy import event
from sqlmodel import select

from app.backend.api.shared.helpers.upload_helper import validation_config_for
from app.backend.enums.errors import ErrorCode
from app.backend.enums.general import UploadPurpose
from app.backend.errors.coded import CodedError
from app.backend.services.files.formats import FormatRegistry, UnsupportedMediaFormat, default_formats
from app.backend.models.media import MediaFile
from app.backend.services import media_manager
from app.backend.services.media_manager import MediaManager
from tests.helpers import make_post, make_user, make_dictionary, make_observation, make_media
from tests.test_media_formats import PNG_BYTES
from tests.test_poster_jobs import MP4_BYTES

OBSERVATION_FORMATS = FormatRegistry(list(default_formats())).restricted(UploadPurpose.OBSERVATION_MEDIA)


@pytest.mark.asyncio
@pytest.mark.parametrize("cross_filesystem", [False, True], ids=["rename", "copy"])
async def test_the_move_into_storage_runs_off_the_event_loop(tmp_path, monkeypatch, cross_filesystem):
    manager = MediaManager(storage_path=str(tmp_path / "media"), temp_path=str(tmp_path / "temp"))
    source = tmp_path / "temp" / "upload.bin"
    source.write_bytes(b"x" * 1024)
    destination = tmp_path / "media" / "2026" / "10" / "11" / "obs_001" / "1_abc.bin"
    calls = []
    real_rename, real_copy2 = Path.rename, shutil.copy2

    def rename(self, target):
        calls.append(("rename", threading.current_thread()))
        if cross_filesystem:
            raise OSError(18, "Invalid cross-device link")
        return real_rename(self, target)

    def copy2(src, dst, *args, **kwargs):
        calls.append(("copy2", threading.current_thread()))
        return real_copy2(src, dst, *args, **kwargs)

    monkeypatch.setattr(Path, "rename", rename)
    monkeypatch.setattr(shutil, "copy2", copy2)
    await manager._move_file_to_storage(source, destination)

    assert destination.read_bytes() == b"x" * 1024 and not source.exists()
    assert [name for name, _ in calls] == (["rename", "copy2"] if cross_filesystem else ["rename"])
    assert all(thread is not threading.main_thread() for _, thread in calls), "a long copy never blocks the loop"


@pytest.mark.asyncio
@pytest.mark.parametrize("failing", ["copy", "replace"])
async def test_a_failed_copy_across_filesystems_leaves_nothing_at_the_destination(tmp_path, monkeypatch, failing):
    """The copy fallback writes a temporary name next to the destination and replaces it into place; on any failure the
    temporary file is removed and the error raised, so no partial file sits under the final name"""
    manager = MediaManager(storage_path=str(tmp_path / "media"), temp_path=str(tmp_path / "temp"))
    source = tmp_path / "temp" / "upload.bin"
    source.write_bytes(b"x" * 4096)
    destination = tmp_path / "media" / "2026" / "10" / "11" / "obs_001" / "1_abc.bin"
    real_copy2 = shutil.copy2

    def rename(self, target):
        raise OSError(18, "Invalid cross-device link")

    def copy2(src, dst, *args, **kwargs):
        if failing == "copy":
            Path(dst).write_bytes(b"x" * 100)  # the share filled up halfway
            raise OSError(28, "No space left on device")
        return real_copy2(src, dst, *args, **kwargs)

    def replace(src, dst):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(Path, "rename", rename)
    monkeypatch.setattr(shutil, "copy2", copy2)
    if failing == "replace":
        monkeypatch.setattr(os, "replace", replace)
    with pytest.raises(OSError):
        await manager._move_file_to_storage(source, destination)

    assert not destination.exists()
    assert list(destination.parent.iterdir()) == [], "no temporary file is left behind"
    assert source.read_bytes() == b"x" * 4096, "the source stays for the caller's cleanup"


@pytest.mark.asyncio
async def test_a_move_the_storage_refuses_removes_the_row(db, tmp_path, monkeypatch):
    """An OSError of the move (raised in its thread) is still media_store_failed, and no row points at a missing file"""
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, post_id=post.id)
    observation = await make_observation(db, user, post, ids["signal_type_id"])
    manager = MediaManager(storage_path=str(tmp_path / "media"), temp_path=str(tmp_path / "temp"))
    source = tmp_path / "temp" / "upload_clip.mp4"
    source.write_bytes(MP4_BYTES)

    def storage_full(src: Path, destination: Path) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(media_manager, "_move_into_place", storage_full)
    with pytest.raises(CodedError) as refused:
        await manager.store_file_from_path(db, observation.id, str(source), "clip.mp4", "video", user_id=user.id)
    assert refused.value.code == ErrorCode.MEDIA_STORE_FAILED
    assert (await db.exec(select(MediaFile).where(MediaFile.observation_id == observation.id))).all() == []


def _spec(filename: str):
    return OBSERVATION_FORMATS.resolve(UploadPurpose.OBSERVATION_MEDIA, filename)


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [b"\x01\x02\x03\x04" * 64, b"plain words, not a photo\n" * 8],
                         ids=["unknown-bytes", "text"])
async def test_the_detected_type_decides_never_the_extension(db, tmp_path, data):
    """The bytes are sniffed once (libmagic): bytes it cannot name are application/octet-stream, never guessed
    from the extension, so a .jpg that is not a JPEG is 415 media_content_type_not_accepted"""
    manager = MediaManager(storage_path=str(tmp_path / "media"), temp_path=str(tmp_path / "temp"))
    source = tmp_path / "temp" / "upload_photo.jpg"
    source.write_bytes(data)
    with pytest.raises(UnsupportedMediaFormat) as refused:
        await manager.store_file_from_path(db, 1, str(source), "photo.jpg", "image",
                                           validation_config=validation_config_for(_spec("photo.jpg")))
    assert refused.value.code == ErrorCode.MEDIA_CONTENT_TYPE_NOT_ACCEPTED
    assert refused.value.params["content_type"] in ("application/octet-stream", "text/plain")
    assert (await db.exec(select(MediaFile))).all() == []


@pytest.mark.asyncio
async def test_a_stored_file_keeps_the_detected_type(db, tmp_path):
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, post_id=post.id)
    observation = await make_observation(db, user, post, ids["signal_type_id"])
    manager = MediaManager(storage_path=str(tmp_path / "media"), temp_path=str(tmp_path / "temp"))
    source = tmp_path / "temp" / "upload_photo.png"
    source.write_bytes(PNG_BYTES)
    spec = _spec("photo.png")
    media_id = await manager.store_file_from_path(db, observation.id, str(source), "Photo.PNG", "image",
                                                  user_id=user.id, validation_config=validation_config_for(spec))
    row = (await db.exec(select(MediaFile).where(MediaFile.id == media_id))).one()
    assert (row.mime_type, row.file_extension) == ("image/png", ".png")


def test_dead_upload_code_is_gone():
    """One sniffer: no second MIME table, no magic-byte checks, no unused helpers on the manager"""
    import importlib.util
    assert importlib.util.find_spec("app.backend.utils.mime_detector") is None
    for name in ("_validate_content_headers", "_is_valid_image_header", "_is_valid_video_header",
                 "check_file_integrity", "repair_file", "cleanup_orphaned_files", "validate_full_path_length",
                 "_detect_mime_from_path"):
        assert not hasattr(MediaManager, name), name
    for name in ("FileValidationResult", "FileIntegrityCheck", "MAX_FILE_SIZE"):
        assert not hasattr(media_manager, name), name
    assert set(media_manager.FileValidationConfig.model_fields) == {
        "max_size", "allowed_extensions", "allowed_mime_types", "content_type", "sanitize_filename"}


@pytest.mark.asyncio
async def test_storage_statistics_count_in_sql_and_walk_the_disk_off_the_event_loop(db, engine, tmp_path, monkeypatch):
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, post_id=post.id)
    observation = await make_observation(db, user, post, ids["signal_type_id"])
    await make_media(db, observation, user, file_size=100)
    await make_media(db, observation, user, file_size=200)
    await make_media(db, observation, user, media_type="video", file_size=1000)
    await make_media(db, observation, user, file_size=10, is_deleted=True)
    await make_media(db, observation, user, media_type="video", file_size=20, is_deleted=True)

    storage = tmp_path / "media"
    manager = MediaManager(storage_path=str(storage), temp_path=str(tmp_path / "temp"))
    (storage / "2026" / "obs_001").mkdir(parents=True)
    (storage / "2026" / "obs_001" / "1_a.png").write_bytes(b"x" * 5)
    (storage / "loose.bin").write_bytes(b"y" * 7)

    walk_threads = []
    real_walk = os.walk

    def walk(*args, **kwargs):
        walk_threads.append(threading.current_thread())
        return real_walk(*args, **kwargs)

    monkeypatch.setattr(os, "walk", walk)
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    try:
        stats = await manager.get_storage_statistics(db)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", record)

    assert stats == {
        "total_files": 5, "total_file_size_bytes": 1330,
        "active_count": 3, "active_size_bytes": 1300,
        "deleted_count": 2, "deleted_size_bytes": 30,
        "media_types": {"image": 2, "video": 1},
        "filesystem_files": 2, "filesystem_size_bytes": 12,
    }
    assert walk_threads and all(thread is not threading.main_thread() for thread in walk_threads), (
        "the filesystem walk runs off the event loop"
    )
    media_reads = [statement for statement in statements if "media_files" in statement]
    assert media_reads, statements
    assert all("count(" in statement.lower() and "file_hash" not in statement for statement in media_reads), (
        "aggregates in SQL, never every media row"
    )


@pytest.mark.asyncio
async def test_storage_statistics_of_an_empty_database(db, tmp_path):
    manager = MediaManager(storage_path=str(tmp_path / "media"), temp_path=str(tmp_path / "temp"))
    assert await manager.get_storage_statistics(db) == {
        "total_files": 0, "total_file_size_bytes": 0,
        "active_count": 0, "active_size_bytes": 0,
        "deleted_count": 0, "deleted_size_bytes": 0,
        "media_types": {},
        "filesystem_files": 0, "filesystem_size_bytes": 0,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("filename, media_type, sniffed, canonical", [
    ("clip.mkv", "video", "video/x-matroska", "video/matroska"),
    ("tone.wav", "audio", "audio/x-wav", "audio/wav"),
    ("photo.jpg", "image", "image/pjpeg", "image/jpeg"),
])
@pytest.mark.parametrize("enable_nginx", [True, False])
async def test_an_alias_upload_stores_the_canonical_type(db, tmp_path, monkeypatch, filename, media_type, sniffed,
                                                         canonical, enable_nginx):
    """The sniffed type only decides acceptance (it may be an alias of the format); the row keeps the format's
    canonical name, which download and stream serve (X-Accel-Redirect and the API's own streaming alike)"""
    from app.backend.api.shared.helpers.media_helper import original_file_response
    from app.backend.config import settings

    monkeypatch.setattr(settings.storage, "upload_path", str(tmp_path / "media"))
    monkeypatch.setattr(settings.storage, "enable_nginx", enable_nginx)
    monkeypatch.setattr(media_manager, "detect_content_type", lambda path: sniffed)
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, post_id=post.id)
    observation = await make_observation(db, user, post, ids["signal_type_id"])
    manager = MediaManager(storage_path=str(tmp_path / "media"), temp_path=str(tmp_path / "temp"))
    source = tmp_path / "temp" / f"upload_{filename}"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(PNG_BYTES if media_type == "image" else b"\x1a\x45\xdf\xa3" + b"\x00" * 64)
    media_id = await manager.store_file_from_path(db, observation.id, str(source), filename, media_type,
                                                  user_id=user.id, validation_config=validation_config_for(_spec(filename)))
    row = (await db.exec(select(MediaFile).where(MediaFile.id == media_id))).one()
    assert row.mime_type == canonical and type(row.mime_type) is str
    response = original_file_response(row, "inline")
    assert response.headers["content-type"] == canonical


@pytest.mark.asyncio
async def test_a_refused_sniffed_type_is_named_as_sniffed(db, tmp_path, monkeypatch):
    """The refusal names what the bytes are, not the format's name"""
    monkeypatch.setattr(media_manager, "detect_content_type", lambda path: "video/webm")
    manager = MediaManager(storage_path=str(tmp_path / "media"), temp_path=str(tmp_path / "temp"))
    source = tmp_path / "temp" / "upload_clip.mkv"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * 64)
    with pytest.raises(UnsupportedMediaFormat) as refused:
        await manager.store_file_from_path(db, 1, str(source), "clip.mkv", "video",
                                           validation_config=validation_config_for(_spec("clip.mkv")))
    assert refused.value.params["content_type"] == "video/webm"
