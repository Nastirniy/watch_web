"""Poster job store on the media_files row and the media worker loop, on in-memory sqlite."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlmodel import select

from app.backend.config import settings
from app.backend.models.media import MediaFile
from app.backend.services.media import jobs
from app.backend.services.media.posters import PosterError
from app.backend.services.media_manager import MediaManager
from app.backend.workers.media_worker import MediaWorker
from tests.helpers import make_post, make_user, make_dictionary, make_observation, make_media

# The first bytes of an MP4 file: enough for the MIME detection and the header check of an upload
MP4_BYTES = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + bytes(200)


async def _videos(db, count: int):
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, post_id=post.id)
    observation = await make_observation(db, user, post, ids["signal_type_id"])
    return [await make_media(db, observation, user, media_type="video") for _ in range(count)]


async def _reload(db, media_id: int) -> MediaFile:
    return await db.get(MediaFile, media_id, populate_existing=True)


# ----------------------------------------------------------------- claiming

@pytest.mark.asyncio
async def test_claim_marks_processing_and_is_exclusive(db):
    first, second = await _videos(db, 2)

    claimed = await jobs.claim_next_poster(db, "w1", lease_seconds=300)
    assert claimed.id == first.id
    assert claimed.poster_status == "processing" and claimed.poster_worker == "w1" and claimed.poster_attempts == 1
    assert claimed.poster_locked_until is not None

    other = await jobs.claim_next_poster(db, "w2", lease_seconds=300)
    assert other.id == second.id, "two workers get distinct rows"
    assert await jobs.claim_next_poster(db, "w3") is None


@pytest.mark.asyncio
async def test_expired_lease_is_reclaimed_and_old_worker_cannot_complete(db):
    (video,) = await _videos(db, 1)
    await jobs.claim_next_poster(db, "w1", lease_seconds=300)
    assert await jobs.claim_next_poster(db, "w2") is None, "lease still valid"

    later = datetime.now(timezone.utc) + timedelta(seconds=301)
    reclaimed = await jobs.claim_next_poster(db, "w2", lease_seconds=300, now=later)
    assert reclaimed is not None and reclaimed.poster_worker == "w2" and reclaimed.poster_attempts == 2

    assert await jobs.complete_poster(db, video.id, "w1") is False, "w1 lost the lease"
    assert await jobs.complete_poster(db, video.id, "w2") is True
    row = await _reload(db, video.id)
    assert row.poster_status == "ready" and row.poster_generated_at is not None and row.poster_locked_until is None


@pytest.mark.asyncio
async def test_failure_backs_off_then_fails_permanently(db, monkeypatch):
    monkeypatch.setattr(settings.storage.poster, "max_attempts", 2)
    monkeypatch.setattr(settings.storage.poster, "backoff_seconds", [30, 120])
    (video,) = await _videos(db, 1)

    await jobs.claim_next_poster(db, "w1")
    assert await jobs.fail_poster(db, video.id, "w1", "boom") == "pending"
    row = await _reload(db, video.id)
    assert row.poster_error == "boom" and row.poster_locked_until is not None
    assert await jobs.claim_next_poster(db, "w1") is None, "backoff keeps the row out of the queue"

    after_backoff = datetime.now(timezone.utc) + timedelta(seconds=31)
    assert (await jobs.claim_next_poster(db, "w1", now=after_backoff)) is not None
    assert await jobs.fail_poster(db, video.id, "w1", "boom again") == "failed"
    row = await _reload(db, video.id)
    assert row.poster_status == "failed" and row.poster_attempts == 2
    assert await jobs.claim_next_poster(db, "w1", now=after_backoff + timedelta(days=1)) is None

    assert await jobs.fail_poster(db, video.id, "w1", "late") is None, "not owned any more"


async def _abandoned(db, video: MediaFile, attempts: int, locked_until: datetime) -> None:
    """A claim whose worker is gone (ffmpeg took it down, out of memory): it recorded nothing, its lease runs out"""
    video.poster_status, video.poster_worker, video.poster_attempts = "processing", "media@gone:1", attempts
    video.poster_locked_until = locked_until
    db.add(video)
    await db.commit()


@pytest.mark.asyncio
async def test_an_expired_claim_on_the_last_attempt_fails_and_is_never_claimed_again(db, monkeypatch):
    monkeypatch.setattr(settings.storage.poster, "max_attempts", 2)
    (video,) = await _videos(db, 1)
    start = datetime.now(timezone.utc)
    await jobs.claim_next_poster(db, "w1", lease_seconds=300, now=start)
    second = start + timedelta(seconds=301)
    reclaimed = await jobs.claim_next_poster(db, "w2", lease_seconds=300, now=second)
    assert reclaimed.poster_attempts == 2, "an attempt was left: the expired claim is taken again"

    after_last = second + timedelta(seconds=301)
    assert await jobs.claim_next_poster(db, "w3", now=after_last) is None, "no attempt left: never run again"
    assert await jobs.fail_exhausted_posters(db, now=second + timedelta(seconds=299)) == 0, "the lease still runs"
    assert await jobs.fail_exhausted_posters(db, now=after_last) == 1
    row = await _reload(db, video.id)
    assert (row.poster_status, row.poster_attempts, row.poster_locked_until) == ("failed", 2, None)
    assert row.poster_error == jobs.WORKER_STOPPED_ERROR and row.thumbnail_status == "failed"
    assert await jobs.fail_exhausted_posters(db, now=after_last) == 0
    assert await jobs.complete_poster(db, video.id, "w2") is False, "the worker that stopped cannot overwrite it"


@pytest.mark.asyncio
async def test_reconcile_fails_an_abandoned_last_attempt(db, tmp_path, monkeypatch):
    monkeypatch.setattr(settings.storage.poster, "max_attempts", 2)
    abandoned, running, early = await _videos(db, 3)
    now = datetime.now(timezone.utc)
    await _abandoned(db, abandoned, attempts=2, locked_until=now - timedelta(seconds=1))
    await _abandoned(db, running, attempts=2, locked_until=now + timedelta(minutes=5))  # a live worker's last attempt
    await _abandoned(db, early, attempts=1, locked_until=now - timedelta(seconds=1))  # one attempt left

    counts = await jobs.reconcile_posters(db, tmp_path)
    assert counts["failed_abandoned"] == 1
    row = await _reload(db, abandoned.id)
    assert row.poster_status == "failed" and row.poster_error == jobs.WORKER_STOPPED_ERROR
    assert (await _reload(db, running.id)).poster_status == "processing"
    assert (await _reload(db, early.id)).poster_status == "processing"

    counts = await jobs.reconcile_posters(db, tmp_path, retry_failed=True)
    assert counts["failed_abandoned"] == 0 and counts["retried_failed"] == 1
    row = await _reload(db, abandoned.id)
    assert (row.poster_status, row.poster_attempts, row.poster_error) == ("pending", 0, None)


# ----------------------------------------------------------------- upload: the file reaches storage first

async def _scene(db):
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, post_id=post.id)
    observation = await make_observation(db, user, post, ids["signal_type_id"])
    return user, observation


@pytest.mark.asyncio
async def test_a_video_is_claimable_only_after_its_file_is_in_storage(db, session_maker, tmp_path, monkeypatch):
    user, observation = await _scene(db)
    manager = MediaManager(storage_path=str(tmp_path / "media"), temp_path=str(tmp_path / "temp"))
    source = tmp_path / "temp" / "upload_clip.mp4"
    source.write_bytes(MP4_BYTES)
    real_move = manager._move_file_to_storage
    seen = {}

    async def move_while_a_worker_polls(src: Path, destination: Path) -> None:
        # The row is committed, the file is still on its way: a worker polls now and half an hour later
        now = datetime.now(timezone.utc)
        async with session_maker() as worker_session:
            row = (await worker_session.exec(select(MediaFile).where(MediaFile.observation_id == observation.id))).one()
            seen["thumbnail_status"] = row.thumbnail_status
            seen["claims"] = [
                await jobs.claim_next_poster(worker_session, "w1", now=moment)
                for moment in (now, now + timedelta(minutes=30))
            ]
        await real_move(src, destination)

    monkeypatch.setattr(manager, "_move_file_to_storage", move_while_a_worker_polls)
    media_id = await manager.store_file_from_path(db, observation.id, str(source), "clip.mp4", "video", user_id=user.id)

    assert seen == {"thumbnail_status": "pending", "claims": [None, None]}, "the client sees pending, no worker claims"
    row = await _reload(db, media_id)
    assert (row.poster_status, row.thumbnail_status) == ("pending", "pending")
    assert row.poster_locked_until is not None, "the caller releases the hold, with the audit row of the upload"
    assert await jobs.claim_next_poster(db, "w1") is None

    await jobs.release_poster_hold(db, media_id)
    assert (await _reload(db, media_id)).poster_locked_until is None
    claimed = await jobs.claim_next_poster(db, "w1")
    assert claimed.id == media_id and claimed.poster_attempts == 1
    assert claimed.resolve_path(manager.storage_path).read_bytes() == MP4_BYTES


@pytest.mark.asyncio
async def test_an_upload_hold_runs_out_when_the_release_never_comes(db):
    """The API stopped between the INSERT and the commit after the move: the worker takes the row once the hold ran
    out (and records the missing file like any failure); a late release leaves the job it took alone"""
    (video,) = await _videos(db, 1)
    jobs.enqueue_poster(video)
    db.add(video)
    await db.commit()
    assert await jobs.claim_next_poster(db, "w1") is None, "held"

    later = datetime.now(timezone.utc) + jobs.UPLOAD_HOLD + timedelta(seconds=1)
    assert (await jobs.claim_next_poster(db, "w1", now=later)).id == video.id
    await jobs.release_poster_hold(db, video.id)
    row = await _reload(db, video.id)
    assert (row.poster_status, row.poster_worker, row.poster_attempts) == ("processing", "w1", 1)
    assert row.poster_locked_until is not None


@pytest.mark.asyncio
async def test_release_and_reconcile(db, tmp_path):
    ready, untracked, failed = await _videos(db, 3)
    await jobs.claim_next_poster(db, "w1")
    assert await jobs.release_claims(db, "w1") == 1
    assert (await _reload(db, ready.id)).poster_status == "pending"

    await db.execute(
        MediaFile.__table__.update().where(MediaFile.id == ready.id).values(poster_status="ready")
    )
    await db.execute(
        MediaFile.__table__.update().where(MediaFile.id == untracked.id).values(poster_status=None)
    )
    await db.execute(
        MediaFile.__table__.update().where(MediaFile.id == failed.id).values(poster_status="failed", poster_attempts=3)
    )
    await db.commit()

    counts = await jobs.reconcile_posters(db, tmp_path, retry_failed=False)
    assert counts == {"failed_abandoned": 0, "queued_untracked": 1, "requeued_missing": 1, "retried_failed": 0}
    counts = await jobs.reconcile_posters(db, tmp_path, retry_failed=True)
    assert counts == {"failed_abandoned": 0, "queued_untracked": 0, "requeued_missing": 0, "retried_failed": 1}
    assert (await _reload(db, failed.id)).poster_attempts == 0

    poster = (await _reload(db, ready.id)).resolve_poster_path(tmp_path)
    poster.parent.mkdir(parents=True)
    poster.write_bytes(b"x")
    await db.execute(MediaFile.__table__.update().where(MediaFile.id == ready.id).values(poster_status="ready"))
    await db.commit()
    assert (await jobs.reconcile_posters(db, tmp_path))["requeued_missing"] == 0

    counts = await jobs.poster_counts(db)
    assert counts["ready"] == 1 and counts["pending"] == 2


@pytest.mark.asyncio
async def test_heartbeat_and_staleness(db, monkeypatch):
    monkeypatch.setattr(settings.storage.poster, "stale_after_seconds", 120)
    started = datetime.now(timezone.utc)
    await jobs.heartbeat(db, "media@host:1", "media", "host", 1, started, 3, 1, details="idle")
    await jobs.heartbeat(db, "media@host:1", "media", "host", 1, started, 4, 1)
    rows = await jobs.worker_heartbeats(db)
    assert len(rows) == 1 and rows[0].jobs_done == 4
    assert not jobs.is_stale(rows[0].last_seen_at)
    assert jobs.is_stale(rows[0].last_seen_at, now=datetime.now(timezone.utc) + timedelta(seconds=121))
    await jobs.remove_heartbeat(db, "media@host:1")
    assert await jobs.worker_heartbeats(db) == []


# ----------------------------------------------------------------- worker

@pytest.mark.asyncio
async def test_worker_processes_success_and_failure(db, session_maker, tmp_path):
    good, bad = await _videos(db, 2)
    calls = []

    def extractor(source: Path, target: Path) -> None:
        calls.append(source.name)
        if source.name == good.stored_filename:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"jpeg")
        else:
            raise PosterError("no video stream")

    worker = MediaWorker(session_maker, tmp_path, name="media@test:1", extractor=extractor)
    assert await worker.claim_and_run_once() is True
    assert await worker.claim_and_run_once() is True
    assert await worker.claim_and_run_once() is False, "queue drained"
    assert calls == [good.stored_filename, bad.stored_filename]

    good_row = await _reload(db, good.id)
    assert good_row.poster_status == "ready"
    assert good_row.resolve_poster_path(tmp_path).read_bytes() == b"jpeg"
    bad_row = await _reload(db, bad.id)
    assert bad_row.poster_status == "pending" and "no video stream" in bad_row.poster_error
    assert worker.jobs_done == 1 and worker.jobs_failed == 1


@pytest.mark.asyncio
async def test_the_worker_fails_an_abandoned_last_attempt_before_it_claims(db, session_maker, tmp_path, monkeypatch):
    monkeypatch.setattr(settings.storage.poster, "max_attempts", 2)
    abandoned, waiting = await _videos(db, 2)
    await _abandoned(db, abandoned, attempts=2, locked_until=datetime.now(timezone.utc) - timedelta(seconds=1))
    calls = []

    def extractor(source: Path, target: Path) -> None:
        calls.append(source.name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"jpeg")

    worker = MediaWorker(session_maker, tmp_path, name="media@test:1", extractor=extractor)
    assert await worker.claim_and_run_once() is True
    assert await worker.claim_and_run_once() is False, "queue drained"
    assert calls == [waiting.stored_filename], "the video that took the worker down is not run again"

    row = await _reload(db, abandoned.id)
    assert (row.poster_status, row.poster_attempts, row.poster_error) == ("failed", 2, jobs.WORKER_STOPPED_ERROR)
    assert (await _reload(db, waiting.id)).poster_status == "ready"
    assert worker.jobs_done == 1 and worker.jobs_failed == 0
