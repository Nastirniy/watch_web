"""Video storyboard job state, job store and media worker integration on in-memory sqlite."""
import asyncio
import os
import shutil
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.backend.config import settings
from app.backend.models.media import MediaFile
from app.backend.models.storyboard import STORYBOARD_DIR, VideoStoryboard, storyboard_relative_dir
from app.backend.services.media import jobs, storyboard_jobs as sb
from app.backend.services.media.storyboards import (
    StoryboardCancelled,
    StoryboardError,
    StoryboardPlan,
    StoryboardResult,
    plan_storyboard,
)
from app.backend.workers import media_worker
from app.backend.workers.media_worker import MediaWorker
from tests.helpers import make_post, make_user, make_dictionary, make_observation, make_media


@pytest.mark.asyncio
async def test_storyboard_job_status_is_visible_on_media(db):
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, post_id=post.id)
    observation = await make_observation(db, user, post, ids["signal_type_id"])
    with_row = await make_media(db, observation, user, media_type="video")
    without_row = await make_media(db, observation, user, media_type="video")

    db.add(VideoStoryboard(media_id=with_row.id, status="ready"))
    await db.commit()

    assert (await db.get(MediaFile, with_row.id, populate_existing=True)).storyboard_job_status == "ready"
    assert (await db.get(MediaFile, without_row.id, populate_existing=True)).storyboard_job_status is None


# ------------------------------------------------------------- job store

@pytest.fixture
def storyboard_settings(monkeypatch):
    config = settings.storage.storyboard
    monkeypatch.setattr(config, "enabled", True)
    monkeypatch.setattr(config, "min_duration_seconds", 60)
    monkeypatch.setattr(config, "max_attempts", 3)
    monkeypatch.setattr(config, "backoff_seconds", [60, 300, 1800])
    return config


async def _videos(db, count: int, **fields):
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, post_id=post.id)
    observation = await make_observation(db, user, post, ids["signal_type_id"])
    return [await make_media(db, observation, user, media_type="video", **fields) for _ in range(count)]


async def _row(db, media_id: int) -> VideoStoryboard:
    return await db.get(VideoStoryboard, media_id, populate_existing=True)


async def _pending(db, media_id: int) -> None:
    db.add(VideoStoryboard(media_id=media_id, status="pending"))
    await db.commit()


@pytest.mark.asyncio
async def test_enqueue_only_long_videos_with_a_known_duration(db, storyboard_settings):
    long_video, short, unknown, archived = await _videos(db, 4)
    long_video.duration_seconds = 120
    short.duration_seconds = 30
    archived.duration_seconds = 120
    archived.is_deleted = True
    db.add_all([long_video, short, archived])
    await db.commit()
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, post_id=post.id)
    observation = await make_observation(db, user, post, ids["signal_type_id"])
    image = await make_media(db, observation, user, media_type="image", duration_seconds=120)

    assert await sb.enqueue_storyboard(db, long_video) is True
    assert (await _row(db, long_video.id)).status == "pending"
    assert await sb.enqueue_storyboard(db, long_video) is False, "a row exists"
    for media in (short, unknown, image, archived):
        assert await sb.enqueue_storyboard(db, media) is False
        assert await _row(db, media.id) is None

    storyboard_settings.enabled = False
    (other,) = await _videos(db, 1, duration_seconds=120)
    assert await sb.enqueue_storyboard(db, other) is False
    assert await _row(db, other.id) is None


@pytest.mark.asyncio
async def test_newest_videos_are_claimed_first(db, storyboard_settings):
    # A backfill of existing videos (reconciliation) must not hold a new upload back: the highest media id goes first
    older, newer = await _videos(db, 2)
    await _pending(db, older.id)
    await _pending(db, newer.id)

    assert (await sb.claim_next_storyboard(db, "w1")).media_id == newer.id
    assert (await sb.claim_next_storyboard(db, "w2")).media_id == older.id


@pytest.mark.asyncio
async def test_claim_is_exclusive_and_leases_expire(db, storyboard_settings):
    second, first = await _videos(db, 2)  # newest first: "first" is the one claimed first
    await _pending(db, first.id)
    await _pending(db, second.id)

    claimed = await sb.claim_next_storyboard(db, "w1", lease_seconds=300)
    assert claimed.media_id == first.id
    assert claimed.status == "processing" and claimed.worker == "w1" and claimed.attempts == 1
    assert claimed.locked_until is not None
    assert (await sb.claim_next_storyboard(db, "w2")).media_id == second.id
    assert await sb.claim_next_storyboard(db, "w3") is None

    later = datetime.now(timezone.utc) + timedelta(seconds=301)
    reclaimed = await sb.claim_next_storyboard(db, "w3", lease_seconds=300, now=later)
    assert reclaimed.media_id == first.id and reclaimed.worker == "w3" and reclaimed.attempts == 2

    plan = StoryboardPlan(step_seconds=2, frame_count=30, columns=10, rows=10, tile_width=160, sheet_count=1, duration_seconds=60)
    result = StoryboardResult(tile_height=90, sheet_count=1)
    assert await sb.complete_storyboard(db, first.id, "w1", plan, result) is False, "w1 lost the lease"
    assert await sb.renew_storyboard_lease(db, first.id, "w1") is False
    assert await sb.renew_storyboard_lease(db, first.id, "w3") is True
    assert await sb.complete_storyboard(db, first.id, "w3", plan, result) is True
    row = await _row(db, first.id)
    assert row.status == "ready" and row.generated_at is not None and row.locked_until is None
    assert (row.step_seconds, row.frame_count, row.columns, row.rows) == (2, 30, 10, 10)
    assert (row.tile_width, row.tile_height, row.sheet_count) == (160, 90, 1)


@pytest.mark.asyncio
async def test_failure_backs_off_then_fails_permanently(db, storyboard_settings):
    (video,) = await _videos(db, 1)
    await _pending(db, video.id)

    await sb.claim_next_storyboard(db, "w1")
    before = datetime.now(timezone.utc)
    assert await sb.fail_storyboard(db, video.id, "w1", "boom") == "pending"
    row = await _row(db, video.id)
    assert row.error == "boom" and row.status == "pending"
    locked = row.locked_until if row.locked_until.tzinfo else row.locked_until.replace(tzinfo=timezone.utc)
    assert timedelta(seconds=55) < locked - before < timedelta(seconds=65)
    assert await sb.claim_next_storyboard(db, "w1") is None, "backoff keeps the row out of the queue"
    assert await sb.fail_storyboard(db, video.id, "w2", "x") is None, "not the owner"

    far = datetime.now(timezone.utc) + timedelta(hours=1)
    await sb.claim_next_storyboard(db, "w1", now=far)
    assert await sb.fail_storyboard(db, video.id, "w1", "boom", now=far) == "pending"
    await sb.claim_next_storyboard(db, "w1", now=far + timedelta(hours=1))
    assert await sb.fail_storyboard(db, video.id, "w1", "boom") == "failed"
    row = await _row(db, video.id)
    assert row.status == "failed" and row.attempts == 3 and row.locked_until is None
    assert await sb.claim_next_storyboard(db, "w1", now=far + timedelta(days=2)) is None


async def _abandoned(db, media_id: int, attempts: int, locked_until: datetime) -> None:
    """A claim whose worker is gone (ffmpeg took it down, out of memory): it recorded nothing, its lease runs out"""
    row = await _row(db, media_id)
    row.status, row.worker, row.attempts, row.locked_until = "processing", "media@gone:1", attempts, locked_until
    db.add(row)
    await db.commit()


@pytest.mark.asyncio
async def test_an_expired_claim_on_the_last_attempt_fails_and_is_never_claimed_again(db, storyboard_settings):
    storyboard_settings.max_attempts = 2
    (video,) = await _videos(db, 1)
    await _pending(db, video.id)
    start = datetime.now(timezone.utc)
    await sb.claim_next_storyboard(db, "w1", lease_seconds=300, now=start)
    second = start + timedelta(seconds=301)
    reclaimed = await sb.claim_next_storyboard(db, "w2", lease_seconds=300, now=second)
    assert reclaimed.attempts == 2, "an attempt was left: the expired claim is taken again"

    after_last = second + timedelta(seconds=301)
    assert await sb.claim_next_storyboard(db, "w3", now=after_last) is None, "no attempt left: never run again"
    assert await sb.fail_exhausted_storyboards(db, now=second + timedelta(seconds=299)) == 0, "the lease still runs"
    assert await sb.fail_exhausted_storyboards(db, now=after_last) == 1
    row = await _row(db, video.id)
    assert (row.status, row.attempts, row.locked_until, row.error) == ("failed", 2, None, jobs.WORKER_STOPPED_ERROR)
    assert await sb.fail_exhausted_storyboards(db, now=after_last) == 0
    plan = StoryboardPlan(step_seconds=2, frame_count=30, columns=10, rows=10, tile_width=160, sheet_count=1, duration_seconds=60)
    assert await sb.complete_storyboard(db, video.id, "w2", plan, StoryboardResult(tile_height=90, sheet_count=1)) is False


@pytest.mark.asyncio
async def test_failure_with_empty_backoff_list_uses_a_default(db, storyboard_settings):
    storyboard_settings.backoff_seconds = []
    (video,) = await _videos(db, 1)
    await _pending(db, video.id)
    await sb.claim_next_storyboard(db, "w1")
    assert await sb.fail_storyboard(db, video.id, "w1", "boom") == "pending"
    assert (await _row(db, video.id)).locked_until is not None


@pytest.mark.asyncio
async def test_release_returns_claims_to_pending(db, storyboard_settings):
    second, first = await _videos(db, 2)  # newest first: w1 claims "first"
    await _pending(db, first.id)
    await _pending(db, second.id)
    await sb.claim_next_storyboard(db, "w1")
    await sb.claim_next_storyboard(db, "w2")
    assert await sb.release_storyboard_claims(db, "w1") == 1
    row = await _row(db, first.id)
    assert row.status == "pending" and row.locked_until is None
    assert (await _row(db, second.id)).status == "processing"
    assert await sb.storyboard_counts(db) == {"pending": 1, "processing": 1, "ready": 0, "failed": 0}


@pytest.mark.asyncio
async def test_release_gives_the_attempt_back(db, storyboard_settings):
    (video,) = await _videos(db, 1)
    db.add(VideoStoryboard(media_id=video.id, status="pending", attempts=1))
    await db.commit()
    await sb.claim_next_storyboard(db, "w1")

    assert await sb.release_storyboard(db, video.id, "w2") is False, "not the owner"
    assert await sb.release_storyboard(db, video.id, "w1") is True
    row = await _row(db, video.id)
    assert (row.status, row.attempts, row.worker, row.locked_until) == ("pending", 1, None, None)
    assert await sb.release_storyboard(db, video.id, "w1") is False, "no longer claimed"

    row.status, row.worker, row.attempts = "processing", "w1", 0
    db.add(row)
    await db.commit()
    assert await sb.release_storyboard(db, video.id, "w1") is True
    assert (await _row(db, video.id)).attempts == 0, "never below zero"


@pytest.mark.asyncio
async def test_archived_and_disabled_are_never_claimed(db, storyboard_settings):
    archived, live = await _videos(db, 2)
    await _pending(db, archived.id)
    await _pending(db, live.id)
    archived.is_deleted = True
    db.add(archived)
    await db.commit()

    storyboard_settings.enabled = False
    assert await sb.claim_next_storyboard(db, "w1") is None
    storyboard_settings.enabled = True
    assert (await sb.claim_next_storyboard(db, "w1")).media_id == live.id
    assert await sb.claim_next_storyboard(db, "w2") is None


@pytest.mark.asyncio
async def test_fewer_frames_than_planned_never_point_past_the_last_sheet(db, storyboard_settings):
    (video,) = await _videos(db, 1)
    await _pending(db, video.id)
    await sb.claim_next_storyboard(db, "w1")
    plan = StoryboardPlan(step_seconds=2, frame_count=250, columns=10, rows=10, tile_width=160, sheet_count=3, duration_seconds=500)
    assert await sb.complete_storyboard(db, video.id, "w1", plan, StoryboardResult(tile_height=90, sheet_count=2))
    row = await _row(db, video.id)
    assert row.frame_count == 200 and row.sheet_count == 2


def _make_sheets(root: Path, media_id: int, count: int = 1) -> Path:
    directory = root / storyboard_relative_dir(media_id)
    directory.mkdir(parents=True)
    for index in range(count):
        (directory / f"sheet-{index:03d}.jpg").write_bytes(b"x")
    return directory


@pytest.mark.asyncio
async def test_reconcile(db, storyboard_settings, tmp_path):
    ready_no_files, ready_ok, untracked, failed, short = await _videos(db, 5)
    for media, status in ((ready_no_files, "ready"), (ready_ok, "ready"), (failed, "failed")):
        db.add(VideoStoryboard(media_id=media.id, status=status, attempts=3, sheet_count=1, frame_count=5))
    untracked.poster_status = "ready"
    untracked.duration_seconds = 120
    short.poster_status = "ready"
    short.duration_seconds = 30
    db.add_all([untracked, short])
    await db.commit()
    _make_sheets(tmp_path, ready_ok.id)

    counts = await sb.reconcile_storyboards(db, tmp_path)
    assert counts == {
        "failed_abandoned": 0, "queued_missing_rows": 1, "requeued_missing_files": 1, "retried_failed": 0,
        "removed_leftovers": 0,
    }
    assert (await _row(db, ready_no_files.id)).status == "pending"
    assert (await _row(db, ready_ok.id)).status == "ready"
    assert (await _row(db, untracked.id)).status == "pending"
    assert await _row(db, short.id) is None
    assert (await _row(db, failed.id)).status == "failed"

    counts = await sb.reconcile_storyboards(db, tmp_path, retry_failed=True)
    assert counts["retried_failed"] == 1
    row = await _row(db, failed.id)
    assert row.status == "pending" and row.attempts == 0

    storyboard_settings.enabled = False
    assert await sb.reconcile_storyboards(db, tmp_path, retry_failed=True) == {
        "failed_abandoned": 0, "queued_missing_rows": 0, "requeued_missing_files": 0, "retried_failed": 0,
        "removed_leftovers": 0,
    }


@pytest.mark.asyncio
async def test_reconcile_fails_an_abandoned_last_attempt(db, storyboard_settings, tmp_path):
    storyboard_settings.max_attempts = 2
    abandoned, running, early = await _videos(db, 3)
    now = datetime.now(timezone.utc)
    for video, attempts, locked_until in (
        (abandoned, 2, now - timedelta(seconds=1)),
        (running, 2, now + timedelta(minutes=5)),  # a live worker's last attempt
        (early, 1, now - timedelta(seconds=1)),  # one attempt left
    ):
        await _pending(db, video.id)
        await _abandoned(db, video.id, attempts, locked_until)

    counts = await sb.reconcile_storyboards(db, tmp_path)
    assert counts["failed_abandoned"] == 1
    row = await _row(db, abandoned.id)
    assert row.status == "failed" and row.error == jobs.WORKER_STOPPED_ERROR
    assert (await _row(db, running.id)).status == "processing"
    assert (await _row(db, early.id)).status == "processing"

    counts = await sb.reconcile_storyboards(db, tmp_path, retry_failed=True)
    assert counts["failed_abandoned"] == 0 and counts["retried_failed"] == 1
    row = await _row(db, abandoned.id)
    assert (row.status, row.attempts, row.error) == ("pending", 0, None)


@pytest.mark.asyncio
async def test_queue_missing_storyboards_reads_only_the_database(db, storyboard_settings):
    eligible, has_row, short, unknown, poster_pending, archived = await _videos(
        db, 6, duration_seconds=120, poster_status="ready"
    )
    short.duration_seconds = 30
    unknown.duration_seconds = None
    poster_pending.poster_status = "pending"
    archived.is_deleted = True
    db.add_all([short, unknown, poster_pending, archived])
    await db.commit()
    await _pending(db, has_row.id)
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, post_id=post.id)
    observation = await make_observation(db, user, post, ids["signal_type_id"])
    image = await make_media(db, observation, user, media_type="image", duration_seconds=120, poster_status="ready")

    storyboard_settings.enabled = False
    assert await sb.queue_missing_storyboards(db) == 0
    storyboard_settings.enabled = True
    assert await sb.queue_missing_storyboards(db) == 1
    assert (await _row(db, eligible.id)).status == "pending"
    for media in (short, unknown, poster_pending, archived, image):
        assert await _row(db, media.id) is None
    assert await sb.queue_missing_storyboards(db) == 0, "nothing twice"


@pytest.mark.asyncio
async def test_reconcile_removes_only_old_leftover_directories(db, storyboard_settings, tmp_path):
    bucket = tmp_path / STORYBOARD_DIR / "000"
    old_tmp = bucket / "7.tmp-abcd1234"
    old_aside = bucket / "7.old-abcd1234"
    fresh = bucket / "8.tmp-abcd1234"
    keep = bucket / "9"
    for directory in (old_tmp, old_aside, fresh, keep):
        directory.mkdir(parents=True)
        (directory / "sheet-000.jpg").write_bytes(b"x")
    stale = time.time() - storyboard_settings.timeout_seconds - 60
    for directory in (old_tmp, old_aside, keep):
        os.utime(directory, (stale, stale))

    counts = await sb.reconcile_storyboards(db, tmp_path)
    assert counts["removed_leftovers"] == 2
    assert not old_tmp.exists() and not old_aside.exists()
    assert fresh.exists() and keep.exists()


@pytest.mark.asyncio
async def test_reconcile_skips_a_row_another_worker_inserted(db, session_maker, storyboard_settings, monkeypatch, tmp_path):
    raced, other = await _videos(db, 2, duration_seconds=120, poster_status="ready")

    async with session_maker() as session:
        real_commit = session.commit
        raced_once = False

        async def commit_after_another_worker() -> None:
            # Another worker starting at the same time inserts its row after our select, before our insert
            nonlocal raced_once
            if not raced_once:
                raced_once = True
                async with session_maker() as another:
                    another.add(VideoStoryboard(media_id=raced.id, status="pending"))
                    await another.commit()
            await real_commit()

        monkeypatch.setattr(session, "commit", commit_after_another_worker)
        counts = await sb.reconcile_storyboards(session, tmp_path)

    assert raced_once
    assert counts["queued_missing_rows"] == 1, "the duplicate is skipped, the run goes on"
    assert (await _row(db, raced.id)).status == "pending"
    assert (await _row(db, other.id)).status == "pending"


@pytest.mark.asyncio
async def test_reconcile_skips_a_leftover_that_vanishes(db, storyboard_settings, monkeypatch, tmp_path):
    bucket = tmp_path / STORYBOARD_DIR / "000"
    vanishing, removable = bucket / "7.tmp-aaaa0000", bucket / "8.tmp-bbbb0000"
    stale = time.time() - storyboard_settings.timeout_seconds - 60
    for directory in (vanishing, removable):
        directory.mkdir(parents=True)
        (directory / "sheet-000.jpg").write_bytes(b"x")
        os.utime(directory, (stale, stale))

    real_stat = Path.stat

    def stat_then_vanish(self, *args, **kwargs):
        result = real_stat(self, *args, **kwargs)
        if self == vanishing:
            shutil.rmtree(vanishing)  # another worker sweeps it right after this look
        return result

    monkeypatch.setattr(Path, "stat", stat_then_vanish)
    counts = await sb.reconcile_storyboards(db, tmp_path)

    assert counts["removed_leftovers"] == 1
    assert not vanishing.exists() and not removable.exists()


# ----------------------------------------------------------------- worker

# Upper bound of every wait below: a broken worker fails the test instead of hanging the suite
WAIT_SECONDS = 10


def _one_session_at_a_time(session_maker):
    """Session factory for a worker that runs while the test reads and writes.

    The in-memory database is one shared connection (StaticPool): a session closing while
    another one sits between its UPDATE and its COMMIT would roll that UPDATE back. In
    production every session has a connection of its own; here they take turns.
    """
    lock = asyncio.Lock()

    @asynccontextmanager
    async def session():
        async with lock:
            async with session_maker() as s:
                yield s

    return session


async def _wait_until(condition, timeout: float) -> None:
    """Re-check a database condition until it holds; fail after the timeout"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not await condition():
        assert loop.time() < deadline, f"condition not met within {timeout}s"
        await asyncio.sleep(0.01)


def _write_poster(target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"jpeg")


def _write_sheet(target_dir: Path) -> StoryboardResult:
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "sheet-000.jpg").write_bytes(b"jpeg")
    return StoryboardResult(tile_height=90, sheet_count=1)


def _no_storyboard(source: Path, target_dir: Path, plan: StoryboardPlan, cancel: threading.Event) -> StoryboardResult:
    raise AssertionError("no storyboard runs in this test")


@pytest.fixture
def fast_polling(monkeypatch):
    monkeypatch.setattr(settings.storage.poster, "poll_interval_seconds", 0.02)
    monkeypatch.setattr(settings.storage.poster, "idle_poll_interval_seconds", 0.05)


@pytest.mark.asyncio
async def test_a_poster_is_not_delayed_by_a_running_storyboard(
    db, session_maker, storyboard_settings, fast_polling, monkeypatch, tmp_path
):
    monkeypatch.setattr(storyboard_settings, "slots", 1)
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, post_id=post.id)
    observation = await make_observation(db, user, post, ids["signal_type_id"])
    second, first = [  # newest first: "first" is the storyboard the worker claims
        await make_media(db, observation, user, media_type="video", duration_seconds=120, poster_status="ready")
        for _ in range(2)
    ]
    for video in (first, second):
        _write_poster(video.resolve_poster_path(tmp_path))  # reconciliation on start keeps a ready poster on disk
        await _pending(db, video.id)

    started, release = threading.Event(), threading.Event()
    calls = []

    def storyboard_extractor(source: Path, target_dir: Path, plan: StoryboardPlan, cancel: threading.Event) -> StoryboardResult:
        # Runs in a thread (asyncio.to_thread) and holds its place until the test lets it go
        calls.append((source, target_dir, plan))
        started.set()
        if not release.wait(timeout=WAIT_SECONDS):
            raise StoryboardError("the test never released the storyboard")
        return _write_sheet(target_dir)

    sessions = _one_session_at_a_time(session_maker)
    worker = MediaWorker(
        sessions, tmp_path, name="media@test:1", concurrency=2,
        extractor=lambda source, target: _write_poster(target),
        storyboard_extractor=storyboard_extractor,
    )
    run = asyncio.create_task(worker.run())
    try:
        assert await asyncio.to_thread(started.wait, WAIT_SECONDS), "the storyboard never started"
        async with sessions() as session:
            # Short, with known metadata: its poster job neither probes nor queues a storyboard
            new_video = await make_media(
                session, observation, user, media_type="video", duration_seconds=30, playback_type="video/mp4"
            )

        async def poster_ready() -> bool:
            async with sessions() as session:
                return (await session.get(MediaFile, new_video.id)).poster_status == "ready"

        # A few idle poll intervals (0.05 s), with headroom for a slow machine
        await _wait_until(poster_ready, timeout=2.0)
        assert not release.is_set(), "the poster was done while the storyboard still ran"
        async with sessions() as session:
            assert await sb.storyboard_counts(session) == {"pending": 1, "processing": 1, "ready": 0, "failed": 0}, (
                "slots = 1: the second storyboard waits although a place is free"
            )
    finally:
        release.set()
        worker.request_stop()
        await asyncio.wait_for(run, timeout=WAIT_SECONDS)

    [(source, target_dir, plan)] = calls
    assert source == first.resolve_path(tmp_path)
    assert target_dir == tmp_path / storyboard_relative_dir(first.id)
    assert plan == plan_storyboard(120, storyboard_settings)
    row = await _row(db, first.id)
    assert row.status == "ready" and (row.tile_height, row.sheet_count) == (90, 1)
    assert (await _row(db, second.id)).status == "pending", "a stopped worker claims nothing more"
    assert worker.jobs_done == 2 and worker.jobs_failed == 0


@pytest.mark.asyncio
async def test_posters_are_claimed_before_storyboards(
    db, session_maker, storyboard_settings, fast_polling, monkeypatch, tmp_path
):
    """Two places, two pending posters and a pending storyboard: both places go to the posters"""
    monkeypatch.setattr(storyboard_settings, "slots", 1)
    (long_video,) = await _videos(db, 1, duration_seconds=120, poster_status="ready")
    _write_poster(long_video.resolve_poster_path(tmp_path))
    await _pending(db, long_video.id)
    await _videos(db, 2, duration_seconds=30, playback_type="video/mp4")

    both_posters_running, release_posters, storyboard_started = threading.Event(), threading.Event(), threading.Event()
    running = []
    lock = threading.Lock()

    def poster_extractor(source: Path, target: Path) -> None:
        with lock:
            running.append(source.name)
            if len(running) == 2:
                both_posters_running.set()
        if not release_posters.wait(timeout=WAIT_SECONDS):
            raise OSError("the test never released the posters")
        _write_poster(target)

    def storyboard_extractor(source: Path, target_dir: Path, plan: StoryboardPlan, cancel: threading.Event) -> StoryboardResult:
        storyboard_started.set()
        return _write_sheet(target_dir)

    sessions = _one_session_at_a_time(session_maker)
    worker = MediaWorker(
        sessions, tmp_path, name="media@test:1", concurrency=2,
        extractor=poster_extractor, storyboard_extractor=storyboard_extractor,
    )
    run = asyncio.create_task(worker.run())
    try:
        assert await asyncio.to_thread(both_posters_running.wait, WAIT_SECONDS), "both places should go to posters"
        assert not storyboard_started.is_set()
        async with sessions() as session:
            assert (await session.get(VideoStoryboard, long_video.id)).status == "pending"
        release_posters.set()
        assert await asyncio.to_thread(storyboard_started.wait, WAIT_SECONDS), "the storyboard runs after the posters"
    finally:
        release_posters.set()
        worker.request_stop()
        await asyncio.wait_for(run, timeout=WAIT_SECONDS)

    assert (await _row(db, long_video.id)).status == "ready"
    assert worker.jobs_done == 3 and worker.jobs_failed == 0


class _ErrorLog:
    """Stands in for the worker's logger: keeps the error messages, drops the rest"""

    def __init__(self):
        self.errors = []

    def error(self, message, *args, **kwargs):
        self.errors.append(message)

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


@pytest.mark.asyncio
async def test_a_storyboard_filesystem_fault_at_start_leaves_posters_working(
    db, session_maker, storyboard_settings, fast_polling, monkeypatch, tmp_path
):
    # E.g. _derived/storyboards made unreadable by a wrong owner: posters must not depend on storyboards
    storyboards_dir = tmp_path / STORYBOARD_DIR
    (storyboards_dir / "000").mkdir(parents=True)
    real_iterdir = Path.iterdir
    scan_threads = []

    def iterdir(self):
        if self == storyboards_dir:
            scan_threads.append(threading.current_thread())
            raise PermissionError(13, "Permission denied", str(self))
        return real_iterdir(self)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    log = _ErrorLog()
    monkeypatch.setattr(media_worker, "_logger", log)
    (new_video,) = await _videos(db, 1, duration_seconds=30, playback_type="video/mp4")

    sessions = _one_session_at_a_time(session_maker)
    worker = MediaWorker(
        sessions, tmp_path, name="media@test:1", concurrency=2,
        extractor=lambda source, target: _write_poster(target), storyboard_extractor=_no_storyboard,
    )
    run = asyncio.create_task(worker.run())
    try:
        async def poster_ready() -> bool:
            if run.done():
                run.result()  # raises what stopped the worker
            async with sessions() as session:
                return (await session.get(MediaFile, new_video.id)).poster_status == "ready"

        await _wait_until(poster_ready, timeout=WAIT_SECONDS)
    finally:
        worker.request_stop()
        await asyncio.wait_for(run, timeout=WAIT_SECONDS)

    assert scan_threads and all(thread is not threading.main_thread() for thread in scan_threads), (
        "the filesystem scan runs off the event loop"
    )
    assert any("PermissionError" in message for message in log.errors), log.errors


@pytest.mark.asyncio
async def test_the_sweep_queues_a_video_that_became_eligible_while_running(
    db, session_maker, storyboard_settings, fast_polling, monkeypatch, tmp_path
):
    # Poster ready, duration unknown: no row at start. media_tools probe-missing fills the duration later (or an
    # enqueue failed): the periodic sweep queues it without waiting for the next worker start
    monkeypatch.setattr(settings.storage.upload, "sweep_interval_seconds", 0.05)
    (video,) = await _videos(db, 1, poster_status="ready", playback_type="video/mp4")
    _write_poster(video.resolve_poster_path(tmp_path))  # the poster stays ready: no poster job queues it instead
    started = threading.Event()

    def storyboard_extractor(source: Path, target_dir: Path, plan: StoryboardPlan, cancel: threading.Event) -> StoryboardResult:
        started.set()
        return _write_sheet(target_dir)

    sessions = _one_session_at_a_time(session_maker)
    worker = MediaWorker(
        sessions, tmp_path, name="media@test:1", concurrency=2,
        extractor=lambda source, target: _write_poster(target), storyboard_extractor=storyboard_extractor,
    )
    run = asyncio.create_task(worker.run())
    try:
        async def heartbeat_written() -> bool:
            async with sessions() as session:
                return bool(await jobs.worker_heartbeats(session))

        await _wait_until(heartbeat_written, timeout=WAIT_SECONDS)  # started, the reconcile on start is done
        async with sessions() as session:
            assert await session.get(VideoStoryboard, video.id) is None
            media = await session.get(MediaFile, video.id)
            media.duration_seconds = 120.0
            session.add(media)
            await session.commit()

        assert await asyncio.to_thread(started.wait, 2.0), "the sweep never queued the storyboard"
    finally:
        worker.request_stop()
        await asyncio.wait_for(run, timeout=WAIT_SECONDS)

    assert (await _row(db, video.id)).status == "ready"


@pytest.mark.asyncio
async def test_the_worker_fails_an_abandoned_last_storyboard_attempt_before_it_claims(
    db, session_maker, storyboard_settings, fast_polling, tmp_path
):
    (video,) = await _videos(db, 1, duration_seconds=120, poster_status="ready")
    _write_poster(video.resolve_poster_path(tmp_path))
    db.add(VideoStoryboard(media_id=video.id, status="ready", sheet_count=1, frame_count=5))
    await db.commit()
    _make_sheets(tmp_path, video.id)  # the reconcile on start keeps it ready

    sessions = _one_session_at_a_time(session_maker)
    worker = MediaWorker(
        sessions, tmp_path, name="media@test:1", concurrency=2,
        extractor=lambda source, target: _write_poster(target), storyboard_extractor=_no_storyboard,
    )
    run = asyncio.create_task(worker.run())
    try:
        async def heartbeat_written() -> bool:
            async with sessions() as session:
                return bool(await jobs.worker_heartbeats(session))

        await _wait_until(heartbeat_written, timeout=WAIT_SECONDS)  # started, the reconcile on start is done
        async with sessions() as session:
            # A worker took it again later and died on the last attempt (ffmpeg took it down)
            await _abandoned(session, video.id, storyboard_settings.max_attempts,
                             datetime.now(timezone.utc) - timedelta(seconds=1))

        async def failed() -> bool:
            async with sessions() as session:
                return (await session.get(VideoStoryboard, video.id, populate_existing=True)).status == "failed"

        await _wait_until(failed, timeout=2.0)
    finally:
        worker.request_stop()
        await asyncio.wait_for(run, timeout=WAIT_SECONDS)

    row = await _row(db, video.id)
    assert (row.attempts, row.error) == (storyboard_settings.max_attempts, jobs.WORKER_STOPPED_ERROR), (
        "failed before any claim: never run again"
    )
    assert worker.jobs_failed == 0


def test_slots_must_leave_a_place_for_posters(storyboard_settings, monkeypatch, tmp_path):
    def build() -> MediaWorker:
        # The constructor opens no session
        return MediaWorker(
            None, tmp_path, concurrency=2,
            extractor=lambda source, target: None, storyboard_extractor=_no_storyboard,
        )

    monkeypatch.setattr(storyboard_settings, "slots", 2)
    with pytest.raises(ValueError, match=r"storage\.storyboard\.slots.*storage\.poster\.concurrency"):
        build()
    storyboard_settings.enabled = False
    assert build().concurrency == 2


@pytest.mark.asyncio
async def test_a_completed_poster_enqueues_the_storyboard(db, session_maker, storyboard_settings, monkeypatch, tmp_path):
    long_video, short = await _videos(db, 2)
    # The duration becomes known when the poster job probes the file
    durations = {long_video.stored_filename: 120.0, short.stored_filename: 30.0}
    monkeypatch.setattr(
        media_worker, "metadata_for",
        lambda source, mime_type, ffmpeg, ffprobe: {"duration_seconds": durations[source.name]},
    )
    worker = MediaWorker(
        session_maker, tmp_path, name="media@test:1", concurrency=2,
        extractor=lambda source, target: _write_poster(target), storyboard_extractor=_no_storyboard,
    )
    assert await worker.claim_and_run_once() is True
    assert await worker.claim_and_run_once() is True

    row = await _row(db, long_video.id)
    assert row is not None and row.status == "pending"
    assert await _row(db, short.id) is None, "too short for a storyboard"


@pytest.mark.asyncio
async def test_worker_records_a_storyboard_failure(db, session_maker, storyboard_settings, tmp_path):
    (video,) = await _videos(db, 1, duration_seconds=120)
    await _pending(db, video.id)

    def failing(source: Path, target_dir: Path, plan: StoryboardPlan, cancel: threading.Event) -> StoryboardResult:
        raise StoryboardError("no storyboard written: boom")

    worker = MediaWorker(
        session_maker, tmp_path, name="media@test:1", concurrency=2,
        extractor=lambda source, target: None, storyboard_extractor=failing,
    )
    async with session_maker() as session:
        claimed = await sb.claim_next_storyboard(session, worker.name)
    assert await worker.process_storyboard(claimed) is False

    row = await _row(db, video.id)
    assert row.status == "pending" and "boom" in row.error and row.locked_until is not None
    assert worker.jobs_failed == 1 and worker.jobs_done == 0


@pytest.mark.asyncio
async def test_a_running_storyboard_renews_its_lease(db, session_maker, storyboard_settings, monkeypatch, tmp_path):
    # A long storyboard outlives its lease (300 s by default): the worker must keep renewing it while ffmpeg runs
    monkeypatch.setattr(media_worker, "LEASE_RENEW_MIN_SECONDS", 0.05)
    monkeypatch.setattr(storyboard_settings, "lease_seconds", 1)  # renewed every max(0.05, 1 // 2) s
    (video,) = await _videos(db, 1, duration_seconds=120)
    await _pending(db, video.id)
    started, release = threading.Event(), threading.Event()

    def long_running(source: Path, target_dir: Path, plan: StoryboardPlan, cancel: threading.Event) -> StoryboardResult:
        started.set()
        if not release.wait(timeout=WAIT_SECONDS):
            raise StoryboardError("the test never released the storyboard")
        return _write_sheet(target_dir)

    sessions = _one_session_at_a_time(session_maker)
    worker = MediaWorker(
        sessions, tmp_path, name="media@test:1", concurrency=2,
        extractor=lambda source, target: None, storyboard_extractor=long_running,
    )
    async with sessions() as session:
        claimed = await sb.claim_next_storyboard(session, worker.name)
    claimed_until = claimed.locked_until
    task = asyncio.create_task(worker.process_storyboard(claimed))
    try:
        assert await asyncio.to_thread(started.wait, WAIT_SECONDS), "the storyboard never started"

        async def lease_moved_forward() -> bool:
            async with sessions() as session:
                row = await session.get(VideoStoryboard, video.id, populate_existing=True)
                return row.worker == worker.name and row.locked_until > claimed_until

        await _wait_until(lease_moved_forward, timeout=2.0)
    finally:
        release.set()
        assert await asyncio.wait_for(task, timeout=WAIT_SECONDS) is True

    assert (await _row(db, video.id)).status == "ready"


def _stoppable_storyboard(started: threading.Event, stopped_with=StoryboardCancelled):
    """Fake extractor that runs until the worker cancels it, then ends with stopped_with"""

    def extract(source: Path, target_dir: Path, plan: StoryboardPlan, cancel: threading.Event) -> StoryboardResult:
        started.set()
        if not cancel.wait(timeout=WAIT_SECONDS):
            raise StoryboardError("the worker never cancelled the storyboard")
        raise stopped_with("ffmpeg stopped")

    return extract


@pytest.mark.parametrize(
    "stopped_with",
    # A process-group stop signal (systemd) also reaches ffmpeg, which may then fail on its own
    [StoryboardCancelled, StoryboardError],
    ids=["cancelled", "ffmpeg_failed_on_the_stop_signal"],
)
@pytest.mark.asyncio
async def test_stop_cancels_a_running_storyboard_and_gives_it_back(
    db, session_maker, storyboard_settings, fast_polling, monkeypatch, tmp_path, stopped_with
):
    monkeypatch.setattr(storyboard_settings, "slots", 1)
    video, held = await _videos(db, 2, duration_seconds=120, poster_status="ready")
    _write_poster(video.resolve_poster_path(tmp_path))
    db.add(VideoStoryboard(media_id=video.id, status="pending", attempts=1))  # one failed attempt before
    # A poster claim this worker holds without running it: released at the end of run, as before
    held.poster_status, held.poster_worker = "processing", "media@test:1"
    held.poster_locked_until = datetime.now(timezone.utc) + timedelta(minutes=5)
    db.add(held)
    await db.commit()

    started = threading.Event()
    sessions = _one_session_at_a_time(session_maker)
    worker = MediaWorker(
        sessions, tmp_path, name="media@test:1", concurrency=2,
        extractor=lambda source, target: _write_poster(target),
        storyboard_extractor=_stoppable_storyboard(started, stopped_with),
    )
    run = asyncio.create_task(worker.run())
    try:
        assert await asyncio.to_thread(started.wait, WAIT_SECONDS), "the storyboard never started"

        async def heartbeat_written() -> bool:
            async with sessions() as session:
                return [row.name for row in await jobs.worker_heartbeats(session)] == ["media@test:1"]

        await _wait_until(heartbeat_written, timeout=WAIT_SECONDS)
        async with sessions() as session:
            row = await session.get(VideoStoryboard, video.id)
            assert (row.status, row.attempts) == ("processing", 2)

        loop = asyncio.get_running_loop()
        stop_requested = loop.time()
        worker.request_stop()
        await asyncio.wait_for(run, timeout=WAIT_SECONDS)
        # Far below storyboard.timeout_seconds (1800 s) and docker's 90 s stop grace
        assert loop.time() - stop_requested < 3.0, "stop does not wait for a storyboard"
    finally:
        worker.request_stop()
        if not run.done():
            await asyncio.wait_for(run, timeout=WAIT_SECONDS)

    row = await _row(db, video.id)
    assert (row.status, row.attempts, row.worker, row.locked_until) == ("pending", 1, None, None), (
        "given back, the attempt does not count"
    )
    assert (await db.get(MediaFile, held.id, populate_existing=True)).poster_status == "pending"
    assert await jobs.worker_heartbeats(db) == []
    assert worker.jobs_done == 0 and worker.jobs_failed == 0


@pytest.mark.asyncio
async def test_stop_lets_a_running_poster_finish(
    db, session_maker, storyboard_settings, fast_polling, monkeypatch, tmp_path
):
    monkeypatch.setattr(storyboard_settings, "slots", 1)
    (long_video,) = await _videos(db, 1, duration_seconds=120, poster_status="ready")
    _write_poster(long_video.resolve_poster_path(tmp_path))
    await _pending(db, long_video.id)
    (new_video,) = await _videos(db, 1, duration_seconds=30, playback_type="video/mp4")

    poster_started, release_poster, storyboard_started = threading.Event(), threading.Event(), threading.Event()

    def poster_extractor(source: Path, target: Path) -> None:
        poster_started.set()
        if not release_poster.wait(timeout=WAIT_SECONDS):
            raise OSError("the test never released the poster")
        _write_poster(target)

    sessions = _one_session_at_a_time(session_maker)
    worker = MediaWorker(
        sessions, tmp_path, name="media@test:1", concurrency=2,
        extractor=poster_extractor, storyboard_extractor=_stoppable_storyboard(storyboard_started),
    )
    run = asyncio.create_task(worker.run())
    try:
        assert await asyncio.to_thread(poster_started.wait, WAIT_SECONDS), "the poster never started"
        assert await asyncio.to_thread(storyboard_started.wait, WAIT_SECONDS), "the storyboard never started"
        worker.request_stop()

        async def storyboard_given_back() -> bool:
            async with sessions() as session:
                return (await session.get(VideoStoryboard, long_video.id)).status == "pending"

        await _wait_until(storyboard_given_back, timeout=2.0)
        assert not run.done(), "the poster still runs"
        release_poster.set()
        await asyncio.wait_for(run, timeout=WAIT_SECONDS)
    finally:
        release_poster.set()
        worker.request_stop()
        if not run.done():
            await asyncio.wait_for(run, timeout=WAIT_SECONDS)

    assert (await db.get(MediaFile, new_video.id, populate_existing=True)).poster_status == "ready"
    assert (await _row(db, long_video.id)).attempts == 0
    assert worker.jobs_done == 1 and worker.jobs_failed == 0


# ------------------------------------------------------------- operations commands

@pytest.mark.asyncio
async def test_media_tools_storyboard_commands(db, session_maker, storyboard_settings, monkeypatch, tmp_path, capsys):
    from app.backend import database
    from app.backend.cli import media_tools

    async def maker():
        return session_maker

    async def no_close():
        return None

    monkeypatch.setattr(database, "get_session_maker", maker)
    monkeypatch.setattr(database, "close_db", no_close)
    monkeypatch.setattr(media_tools, "_storage_root", lambda: tmp_path)
    ready, failed = await _videos(db, 2, duration_seconds=120, poster_status="ready")
    db.add(VideoStoryboard(media_id=failed.id, status="failed", attempts=3, error="ffmpeg exited with 1"))
    await db.commit()

    assert await media_tools.storyboard_status() == 0
    out = capsys.readouterr().out
    assert "storyboards:" in out and "failed: 1" in out and "pending: 0" in out
    assert "latest errors:" in out and f"media {failed.id} failed attempts=3: ffmpeg exited with 1" in out
    assert await sb.latest_storyboard_errors(db, limit=5) == [(failed.id, "failed", 3, "ffmpeg exited with 1")]

    assert await media_tools.reconcile_storyboards_command(retry_failed=False) == 0
    out = capsys.readouterr().out
    assert "queued_missing_rows: 1" in out and "retried_failed: 0" in out
    assert (await _row(db, ready.id)).status == "pending"
    assert (await _row(db, failed.id)).status == "failed"

    assert await media_tools.reconcile_storyboards_command(retry_failed=True) == 0
    assert "retried_failed: 1" in capsys.readouterr().out
    assert (await _row(db, failed.id)).status == "pending"
