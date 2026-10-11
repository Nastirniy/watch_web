"""Media storage: the move into storage and the storage statistics keep their filesystem work off the event loop,
and the statistics count in SQL instead of loading every media row."""
import os
import shutil
import threading
from pathlib import Path

import pytest
from sqlalchemy import event
from sqlmodel import select

from app.backend.enums.errors import ErrorCode
from app.backend.errors.coded import CodedError
from app.backend.models.media import MediaFile
from app.backend.services import media_manager
from app.backend.services.media_manager import MediaManager
from tests.helpers import make_post, make_user, make_dictionary, make_observation, make_media
from tests.test_poster_jobs import MP4_BYTES


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
