"""Storyboard job store: one row of video_storyboards per video.

Same scheme as the poster queue in jobs.py: workers claim with a conditional
UPDATE (two workers may pick the same candidate, only one update matches, the
other selects again), which needs no SKIP LOCKED and works on MySQL, MariaDB and
the sqlite of the test suite. Every completion or failure is guarded by
worker = <claimant> and status = processing, so a worker whose lease expired
cannot overwrite a newer result. An expired claim is taken again only while
attempts are left; fail_exhausted_storyboards fails the others.
"""
from __future__ import annotations

import asyncio
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from sqlalchemy import and_, case, func, or_, update
from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.config import settings
from app.backend.enums.general import MediaType, PosterStatus, StoryboardJobStatus
from app.backend.models.media import MediaFile
from app.backend.models.storyboard import STORYBOARD_DIR, VideoStoryboard, storyboard_relative_dir
from app.backend.services.media.jobs import WORKER_STOPPED_ERROR, utcnow
from app.backend.services.media.storyboards import StoryboardPlan, StoryboardResult

DEFAULT_BACKOFF_SECONDS = 60
LEFTOVER_MARKERS = (".tmp-", ".old-")


def _eligible(media: MediaFile, min_duration: int) -> bool:
    return (
        media.is_video
        and not media.is_deleted
        and media.duration_seconds is not None
        and media.duration_seconds >= min_duration
    )


async def _insert_pending(session: AsyncSession, media_id: int) -> bool:
    """Insert and commit a pending row; False when another worker inserted it first (the duplicate is skipped)"""
    session.add(VideoStoryboard(media_id=media_id, status=StoryboardJobStatus.PENDING.value))
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        if await session.get(VideoStoryboard, media_id) is None:
            raise  # not the primary key duplicate this handles
        return False
    return True


async def enqueue_storyboard(session: AsyncSession, media: MediaFile) -> bool:
    """Insert a pending row for a long enough, non-archived video that has none; True when it inserted"""
    config = settings.storage.storyboard
    if not config.enabled or not _eligible(media, config.min_duration_seconds):
        return False
    if await session.get(VideoStoryboard, media.id) is not None:
        return False
    return await _insert_pending(session, media.id)


def _expired(now: datetime):
    return and_(VideoStoryboard.status == StoryboardJobStatus.PROCESSING.value, VideoStoryboard.locked_until < now)


def _claimable(now: datetime):
    pending = and_(
        VideoStoryboard.status == StoryboardJobStatus.PENDING.value,
        or_(VideoStoryboard.locked_until.is_(None), VideoStoryboard.locked_until < now),
    )
    retry = and_(_expired(now), VideoStoryboard.attempts < settings.storage.storyboard.max_attempts)
    live_video = VideoStoryboard.media_id.in_(
        select(MediaFile.id).where(MediaFile.is_deleted == False, MediaFile.media_type == MediaType.VIDEO.value)
    )
    return and_(live_video, or_(pending, retry))


async def fail_exhausted_storyboards(session: AsyncSession, now: Optional[datetime] = None) -> int:
    """Mark failed every expired claim on its last attempt: its worker stopped during processing (commits).

    _claimable never takes such a row again; this records why. The worker calls it before it claims, reconcile too.
    """
    now = now or utcnow()
    result = await session.execute(
        update(VideoStoryboard)
        .execution_options(synchronize_session=False)
        .where(_expired(now), VideoStoryboard.attempts >= settings.storage.storyboard.max_attempts)
        .values(status=StoryboardJobStatus.FAILED.value, locked_until=None, error=WORKER_STOPPED_ERROR)
    )
    await session.commit()
    return result.rowcount


async def claim_next_storyboard(
    session: AsyncSession,
    worker: str,
    lease_seconds: Optional[int] = None,
    now: Optional[datetime] = None,
    attempts: int = 3,
) -> Optional[VideoStoryboard]:
    """Atomically claim the newest claimable row for this worker; None when the queue is empty or storyboards are off.

    Newest first (highest media id): a new upload is not held back behind a backfill of existing videos.
    """
    config = settings.storage.storyboard
    if not config.enabled:
        return None
    now = now or utcnow()
    lease = timedelta(seconds=lease_seconds or config.lease_seconds)
    for _ in range(attempts):
        candidate = (await session.exec(
            select(VideoStoryboard.media_id).where(_claimable(now)).order_by(VideoStoryboard.media_id.desc()).limit(1)
        )).first()
        if candidate is None:
            return None
        result = await session.execute(
            update(VideoStoryboard)
            .execution_options(synchronize_session=False)
            .where(VideoStoryboard.media_id == candidate, _claimable(now))
            .values(
                status=StoryboardJobStatus.PROCESSING.value,
                worker=worker,
                locked_until=now + lease,
                attempts=VideoStoryboard.attempts + 1,
            )
        )
        await session.commit()
        if result.rowcount == 1:
            return (await session.exec(
                select(VideoStoryboard).where(VideoStoryboard.media_id == candidate).execution_options(populate_existing=True)
            )).one()
    return None


def _owned(media_id: int, worker: str):
    return and_(
        VideoStoryboard.media_id == media_id,
        VideoStoryboard.worker == worker,
        VideoStoryboard.status == StoryboardJobStatus.PROCESSING.value,
    )


async def renew_storyboard_lease(session: AsyncSession, media_id: int, worker: str, lease_seconds: Optional[int] = None) -> bool:
    lease = timedelta(seconds=lease_seconds or settings.storage.storyboard.lease_seconds)
    result = await session.execute(
        update(VideoStoryboard)
        .execution_options(synchronize_session=False)
        .where(_owned(media_id, worker))
        .values(locked_until=utcnow() + lease)
    )
    await session.commit()
    return result.rowcount == 1


async def complete_storyboard(
    session: AsyncSession,
    media_id: int,
    worker: str,
    plan: StoryboardPlan,
    result: StoryboardResult,
    now: Optional[datetime] = None,
) -> bool:
    """Mark ready and store the layout. False when this worker no longer owns the job (lease lost).

    The layout never points past the frames that were written: fewer sheets than planned
    shorten frame_count to what those sheets hold.
    """
    now = now or utcnow()
    frame_count = min(plan.frame_count, result.sheet_count * plan.columns * plan.rows)
    outcome = await session.execute(
        update(VideoStoryboard)
        .execution_options(synchronize_session=False)
        .where(_owned(media_id, worker))
        .values(
            status=StoryboardJobStatus.READY.value,
            generated_at=now,
            locked_until=None,
            error=None,
            step_seconds=plan.step_seconds,
            frame_count=frame_count,
            columns=plan.columns,
            rows=plan.rows,
            tile_width=plan.tile_width,
            tile_height=result.tile_height,
            sheet_count=result.sheet_count,
        )
    )
    await session.commit()
    return outcome.rowcount == 1


def backoff_for(attempts: int) -> timedelta:
    delays = settings.storage.storyboard.backoff_seconds or [DEFAULT_BACKOFF_SECONDS]
    index = max(0, min(attempts - 1, len(delays) - 1))
    return timedelta(seconds=int(delays[index]))


async def fail_storyboard(
    session: AsyncSession,
    media_id: int,
    worker: str,
    error: str,
    now: Optional[datetime] = None,
) -> Optional[str]:
    """Record a failure: back to pending with a backoff, or failed once max_attempts is reached.

    Returns the new status, or None when this worker no longer owns the job.
    """
    now = now or utcnow()
    row = (await session.exec(
        select(VideoStoryboard).where(VideoStoryboard.media_id == media_id).execution_options(populate_existing=True)
    )).first()
    if row is None or row.worker != worker or row.status != StoryboardJobStatus.PROCESSING.value:
        return None
    if row.attempts >= settings.storage.storyboard.max_attempts:
        new_status = StoryboardJobStatus.FAILED.value
        locked_until = None
    else:
        new_status = StoryboardJobStatus.PENDING.value
        locked_until = now + backoff_for(row.attempts)
    result = await session.execute(
        update(VideoStoryboard)
        .execution_options(synchronize_session=False)
        .where(_owned(media_id, worker))
        .values(status=new_status, locked_until=locked_until, error=error[:500])
    )
    await session.commit()
    return new_status if result.rowcount == 1 else None


async def release_storyboard(session: AsyncSession, media_id: int, worker: str) -> bool:
    """Give one claimed job back without a failure (the worker stopped it): pending at once, the attempt not counted.

    False when this worker no longer owns the job.
    """
    result = await session.execute(
        update(VideoStoryboard)
        .execution_options(synchronize_session=False)
        .where(_owned(media_id, worker))
        .values(
            status=StoryboardJobStatus.PENDING.value,
            locked_until=None,
            worker=None,
            attempts=case((VideoStoryboard.attempts > 0, VideoStoryboard.attempts - 1), else_=0),
        )
    )
    await session.commit()
    return result.rowcount == 1


async def release_storyboard_claims(session: AsyncSession, worker: str) -> int:
    """Return every job this worker still holds to pending (graceful shutdown)"""
    result = await session.execute(
        update(VideoStoryboard)
        .execution_options(synchronize_session=False)
        .where(VideoStoryboard.worker == worker, VideoStoryboard.status == StoryboardJobStatus.PROCESSING.value)
        .values(status=StoryboardJobStatus.PENDING.value, locked_until=None)
    )
    await session.commit()
    return result.rowcount


def _requeue(row: VideoStoryboard) -> None:
    row.status = StoryboardJobStatus.PENDING.value
    row.attempts = 0
    row.locked_until = None
    row.worker = None
    row.error = None
    row.generated_at = None


def _without_sheets(storage_root: Path, layouts: List[Tuple[int, Optional[int]]]) -> List[int]:
    """Media ids among (media_id, sheet_count) whose sheet files are not all on disk (filesystem only)"""
    missing = []
    for media_id, sheet_count in layouts:
        directory = storage_root / storyboard_relative_dir(media_id)
        sheets = max(sheet_count or 0, 1)
        if not all((directory / f"sheet-{index:03d}.jpg").is_file() for index in range(sheets)):
            missing.append(media_id)
    return missing


def _remove_leftovers(storage_root: Path, older_than_seconds: int) -> int:
    """Delete temporary and set-aside directories of interrupted runs, never one a worker may still be using"""
    base = storage_root / STORYBOARD_DIR
    if not base.is_dir():
        return 0
    cutoff = time.time() - older_than_seconds
    removed = 0
    for bucket in base.iterdir():
        if not bucket.is_dir():
            continue
        for entry in bucket.iterdir():
            if not entry.is_dir() or not any(marker in entry.name for marker in LEFTOVER_MARKERS):
                continue
            try:
                modified = entry.stat().st_mtime
            except FileNotFoundError:
                continue  # another worker swept it after the listing
            if modified < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
                removed += 1
    return removed


async def queue_missing_storyboards(session: AsyncSession) -> int:
    """Insert a pending row for every non-archived video with a ready poster and a long enough known duration that
    has none; the number inserted. Database only (the worker runs it every sweep interval); nothing while storyboards
    are off. Safe while other workers run: rows are inserted one by one, one another worker inserted meanwhile is skipped.
    """
    config = settings.storage.storyboard
    if not config.enabled:
        return 0
    candidates = (await session.exec(
        select(MediaFile.id)
        .where(
            MediaFile.is_deleted == False,
            MediaFile.media_type == MediaType.VIDEO.value,
            MediaFile.poster_status == PosterStatus.READY.value,
            MediaFile.duration_seconds.is_not(None),
            MediaFile.duration_seconds >= config.min_duration_seconds,
            MediaFile.id.not_in(select(VideoStoryboard.media_id)),
        )
        .order_by(MediaFile.id)
    )).all()
    queued = 0
    for media_id in candidates:
        if await _insert_pending(session, media_id):
            queued += 1
    return queued


async def reconcile_storyboards(session: AsyncSession, storage_root: Path, retry_failed: bool = False) -> Dict[str, int]:
    """Repair storyboard state against the filesystem and the media rows (nothing while storyboards are off).

    - expired claims on their last attempt become failed (fail_exhausted_storyboards, first)
    - a non-archived video with a ready poster and a long enough known duration, without a row, gets a pending row
      (queue_missing_storyboards: it needs no filesystem)
    - ready rows whose sheets are missing become pending
    - failed rows become pending with attempts reset when retry_failed is true
    - temporary and set-aside directories older than the extraction timeout are removed
    A processing row with an expired lease and attempts left needs nothing here: it is claimable by itself.
    The filesystem is read in a thread (asyncio.to_thread), never on the event loop; its OSError reaches the caller.
    Safe while other workers start: a row another worker inserted meanwhile is skipped; a leftover another
    worker removed meanwhile is skipped.
    """
    counts = {
        "failed_abandoned": 0, "queued_missing_rows": 0, "requeued_missing_files": 0, "retried_failed": 0,
        "removed_leftovers": 0,
    }
    config = settings.storage.storyboard
    if not config.enabled:
        return counts
    counts["failed_abandoned"] = await fail_exhausted_storyboards(session)
    counts["queued_missing_rows"] = await queue_missing_storyboards(session)
    live_videos = set((await session.exec(
        select(MediaFile.id).where(MediaFile.is_deleted == False, MediaFile.media_type == MediaType.VIDEO.value)
    )).all())
    rows = [
        row
        for row in (await session.exec(select(VideoStoryboard).execution_options(populate_existing=True))).all()
        if row.media_id in live_videos
    ]
    ready = [(row.media_id, row.sheet_count) for row in rows if row.status == StoryboardJobStatus.READY.value]
    without_sheets = set(await asyncio.to_thread(_without_sheets, storage_root, ready))
    for row in rows:
        if row.media_id in without_sheets:
            _requeue(row)
            counts["requeued_missing_files"] += 1
        elif row.status == StoryboardJobStatus.FAILED.value and retry_failed:
            _requeue(row)
            counts["retried_failed"] += 1
    await session.commit()
    counts["removed_leftovers"] = await asyncio.to_thread(_remove_leftovers, storage_root, config.timeout_seconds)
    return counts


async def storyboard_counts(session: AsyncSession) -> Dict[str, int]:
    """Rows by job status"""
    rows = (await session.exec(select(VideoStoryboard.status, func.count()).group_by(VideoStoryboard.status))).all()
    counts = {status.value: 0 for status in StoryboardJobStatus}
    for status_value, count in rows:
        counts[status_value] = int(count)
    return counts


async def latest_storyboard_errors(session: AsyncSession, limit: int = 5) -> List[Tuple[int, str, int, str]]:
    """(media_id, status, attempts, error) of rows with a non-empty error, highest media_id first (operators only)"""
    rows = (await session.exec(
        select(VideoStoryboard.media_id, VideoStoryboard.status, VideoStoryboard.attempts, VideoStoryboard.error)
        .where(VideoStoryboard.error.is_not(None), VideoStoryboard.error != "")
        .order_by(VideoStoryboard.media_id.desc())
        .limit(limit)
    )).all()
    return [(media_id, status_value, attempts, error) for media_id, status_value, attempts, error in rows]
