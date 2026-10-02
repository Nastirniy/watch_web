"""Poster job store on the media_files row and the media worker loop, on in-memory sqlite."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.backend.config import settings
from app.backend.models.media import MediaFile
from app.backend.services.media import jobs
from app.backend.services.media.posters import PosterError
from app.backend.workers.media_worker import MediaWorker
from tests.helpers import make_post, make_user, make_dictionary, make_observation, make_media


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
    assert counts == {"queued_untracked": 1, "requeued_missing": 1, "retried_failed": 0}
    counts = await jobs.reconcile_posters(db, tmp_path, retry_failed=True)
    assert counts == {"queued_untracked": 0, "requeued_missing": 0, "retried_failed": 1}
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
