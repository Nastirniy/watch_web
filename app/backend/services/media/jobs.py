"""Poster job store: the queue is the media_files row itself.

A video row is enqueued by setting poster_status = pending in the same
transaction that creates it, so a job can never be lost. The job is held
(poster_locked_until ahead) until the commit after the file reached storage
releases it: the client sees pending, no worker claims a file still on its way.
Workers claim with a conditional UPDATE: two workers may pick the same
candidate, only one update matches, the other selects again. This needs no
SKIP LOCKED and works on MySQL 5.7 and 8, MariaDB and the sqlite used by the
test suite.

Every completion or failure is guarded by poster_worker = <claimant> and
poster_status = processing, so a worker whose lease expired cannot overwrite
a newer result. An expired claim is taken again only while attempts are left:
a video whose extraction takes the worker down (out of memory, a crash) is
failed by fail_exhausted_posters, never run forever.

An outcome a page shows (ready, failed, unsupported: thumbnail_status) publishes media.changed processed in its
own transaction (publish_processed); a failure that goes back to pending, a claim and a lease publish nothing.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

from sqlalchemy import and_, func, or_, update
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.config import settings
from app.backend.enums.events import MediaChange
from app.backend.enums.general import MediaType, PosterStatus
from app.backend.models.media import MediaFile
from app.backend.models.observation import Observation
from app.backend.models.worker import WorkerHeartbeat
from app.backend.services.events import publish_media_changed

# Longer than any move of an upload into storage (a rename, or a copy across filesystems of the largest file).
# Released in the upload's last transaction, after the move, with its audit row (record_upload in
# api/shared/helpers/upload_helper.py); it runs out only when the API stopped before that commit or the commit failed,
# and the worker then takes the row like any other (a file that never arrived fails as missing).
UPLOAD_HOLD = timedelta(hours=1)

# Error of a job whose last attempt ended with its worker (killed, out of memory, the host restarted); posters and
# storyboards
WORKER_STOPPED_ERROR = "the worker stopped during processing on the last attempt"


async def publish_processed(session: AsyncSession, media_ids: Iterable[int]) -> None:
    """media.changed processed for each of these media (of an observation), last in the transaction that changed
    their poster or storyboard: one read of where they are (the post of the observation), after the change's
    locks. Media of old data without an observation: at the root. Nothing to read while events are off."""
    ids = sorted(set(media_ids))
    if not ids or not settings.events.enabled:
        return
    places = (await session.exec(
        select(MediaFile.id, MediaFile.observation_id, Observation.post_id)
        .outerjoin(Observation, Observation.id == MediaFile.observation_id)
        .where(MediaFile.id.in_(ids))
    )).all()
    for media_id, observation_id, post_id in places:
        publish_media_changed(session, media_id, observation_id, post_id, MediaChange.PROCESSED)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def enqueue_poster(media: MediaFile) -> None:
    """Mark a freshly created video row for poster extraction (call before the creating commit).

    The job is held for UPLOAD_HOLD: call release_poster_hold in the commit after the file is in storage.
    """
    if media.is_video:
        media.poster_status = PosterStatus.PENDING.value
        media.poster_attempts = 0
        media.poster_locked_until = utcnow() + UPLOAD_HOLD
        media.poster_worker = None
        media.poster_error = None
        media.poster_generated_at = None


async def release_poster_hold(session: AsyncSession, media_id: int, commit: bool = True) -> None:
    """The file of a new video is in storage: workers may claim its poster job from now on (commits; commit=False
    leaves the UPDATE to the caller's transaction, the upload's last one with its audit row).

    Only the untouched hold is released; a job a worker took after the hold ran out goes on as it is.
    """
    await session.execute(
        update(MediaFile)
        .execution_options(synchronize_session=False)
        .where(
            MediaFile.id == media_id,
            MediaFile.poster_status == PosterStatus.PENDING.value,
            MediaFile.poster_attempts == 0,
        )
        .values(poster_locked_until=None)
    )
    if commit:
        await session.commit()


def _expired(now: datetime):
    return and_(MediaFile.poster_status == PosterStatus.PROCESSING.value, MediaFile.poster_locked_until < now)


def _claimable(now: datetime):
    pending = and_(
        MediaFile.poster_status == PosterStatus.PENDING.value,
        or_(MediaFile.poster_locked_until.is_(None), MediaFile.poster_locked_until < now),
    )
    retry = and_(_expired(now), MediaFile.poster_attempts < settings.storage.poster.max_attempts)
    return and_(
        MediaFile.is_deleted == False,
        MediaFile.media_type == MediaType.VIDEO.value,
        or_(pending, retry),
    )


async def fail_exhausted_posters(session: AsyncSession, now: Optional[datetime] = None) -> int:
    """Mark failed every expired claim on its last attempt: its worker stopped during processing (commits).

    _claimable never takes such a row again; this records why. The worker calls it before it claims, reconcile too.
    """
    now = now or utcnow()
    exhausted = (_expired(now), MediaFile.poster_attempts >= settings.storage.poster.max_attempts)
    # Locked first (a current read, no snapshot), so the event names exactly the rows this update fails; bounded by
    # the claims of workers that stopped. A second worker waits here and then finds nothing
    ids = list((await session.exec(select(MediaFile.id).where(*exhausted).with_for_update())).all())
    count = 0
    if ids:
        result = await session.execute(
            update(MediaFile)
            .execution_options(synchronize_session=False)
            .where(MediaFile.id.in_(ids), *exhausted)
            .values(poster_status=PosterStatus.FAILED.value, poster_locked_until=None, poster_error=WORKER_STOPPED_ERROR)
        )
        count = result.rowcount
        await publish_processed(session, ids)
    await session.commit()
    return count


async def claim_next_poster(
    session: AsyncSession,
    worker: str,
    lease_seconds: Optional[int] = None,
    now: Optional[datetime] = None,
    attempts: int = 3,
) -> Optional[MediaFile]:
    """Atomically claim the oldest claimable video row for this worker, or None when the queue is empty"""
    now = now or utcnow()
    lease = timedelta(seconds=lease_seconds or settings.storage.poster.lease_seconds)
    for _ in range(attempts):
        candidate = (await session.exec(
            select(MediaFile.id).where(_claimable(now)).order_by(MediaFile.id).limit(1)
        )).first()
        if candidate is None:
            return None
        result = await session.execute(
            update(MediaFile)
            .execution_options(synchronize_session=False)
            .where(MediaFile.id == candidate, _claimable(now))
            .values(
                poster_status=PosterStatus.PROCESSING.value,
                poster_worker=worker,
                poster_locked_until=now + lease,
                poster_attempts=MediaFile.poster_attempts + 1,
            )
        )
        await session.commit()
        if result.rowcount == 1:
            return (await session.exec(
                select(MediaFile).where(MediaFile.id == candidate).execution_options(populate_existing=True)
            )).one()
    return None


async def renew_lease(session: AsyncSession, media_id: int, worker: str, lease_seconds: Optional[int] = None) -> bool:
    lease = timedelta(seconds=lease_seconds or settings.storage.poster.lease_seconds)
    result = await session.execute(
        update(MediaFile)
        .execution_options(synchronize_session=False)
        .where(
            MediaFile.id == media_id,
            MediaFile.poster_worker == worker,
            MediaFile.poster_status == PosterStatus.PROCESSING.value,
        )
        .values(poster_locked_until=utcnow() + lease)
    )
    await session.commit()
    return result.rowcount == 1


async def complete_poster(
    session: AsyncSession, media_id: int, worker: str, now: Optional[datetime] = None, metadata: Optional[Dict] = None
) -> bool:
    """Mark ready (optionally storing probed metadata). False when this worker no longer owns the job (lease lost)."""
    now = now or utcnow()
    result = await session.execute(
        update(MediaFile)
        .execution_options(synchronize_session=False)
        .where(
            MediaFile.id == media_id,
            MediaFile.poster_worker == worker,
            MediaFile.poster_status == PosterStatus.PROCESSING.value,
        )
        .values(
            poster_status=PosterStatus.READY.value,
            poster_generated_at=now,
            poster_locked_until=None,
            poster_error=None,
            **(metadata or {}),
        )
    )
    if result.rowcount == 1:
        await publish_processed(session, [media_id])
    await session.commit()
    return result.rowcount == 1


async def mark_poster_unsupported(
    session: AsyncSession, media_id: int, worker: Optional[str] = None, metadata: Optional[Dict] = None,
    commit: bool = True,
) -> bool:
    """No poster can exist: a probe found no video stream in the file. Unsupported rows are never claimed.

    Without worker it replaces a pending job (the upload that probed the file); a job a worker already claimed
    belongs to that worker. With worker it ends the job this worker holds, storing what its probe found.
    False when the job is not in that state. Commits; commit=False leaves it to the caller's transaction.
    """
    if worker is None:
        owned = (MediaFile.poster_status == PosterStatus.PENDING.value,)
    else:
        owned = (MediaFile.poster_worker == worker, MediaFile.poster_status == PosterStatus.PROCESSING.value)
    result = await session.execute(
        update(MediaFile)
        .execution_options(synchronize_session=False)
        .where(MediaFile.id == media_id, *owned)
        .values(poster_status=PosterStatus.UNSUPPORTED.value, poster_locked_until=None, poster_error=None, **(metadata or {}))
    )
    if worker is not None and result.rowcount == 1:
        # The worker's outcome; the upload's probe (no worker) is part of the upload, which publishes created
        await publish_processed(session, [media_id])
    if commit:
        await session.commit()
    return result.rowcount == 1


def backoff_for(attempts: int) -> timedelta:
    delays = settings.storage.poster.backoff_seconds or [30]
    index = max(0, min(attempts - 1, len(delays) - 1))
    return timedelta(seconds=int(delays[index]))


async def fail_poster(
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
        select(MediaFile).where(MediaFile.id == media_id).execution_options(populate_existing=True)
    )).first()
    if row is None or row.poster_worker != worker or row.poster_status != PosterStatus.PROCESSING.value:
        return None
    max_attempts = settings.storage.poster.max_attempts
    if row.poster_attempts >= max_attempts:
        new_status = PosterStatus.FAILED.value
        locked_until = None
    else:
        new_status = PosterStatus.PENDING.value
        locked_until = now + backoff_for(row.poster_attempts)
    result = await session.execute(
        update(MediaFile)
        .execution_options(synchronize_session=False)
        .where(
            MediaFile.id == media_id,
            MediaFile.poster_worker == worker,
            MediaFile.poster_status == PosterStatus.PROCESSING.value,
        )
        .values(poster_status=new_status, poster_locked_until=locked_until, poster_error=error[:500])
    )
    if result.rowcount == 1 and new_status == PosterStatus.FAILED.value:
        await publish_processed(session, [media_id])     # back to pending: a page sees nothing new
    await session.commit()
    return new_status if result.rowcount == 1 else None


async def release_claims(session: AsyncSession, worker: str) -> int:
    """Return every job this worker still holds to pending (graceful shutdown)"""
    result = await session.execute(
        update(MediaFile)
        .execution_options(synchronize_session=False)
        .where(MediaFile.poster_worker == worker, MediaFile.poster_status == PosterStatus.PROCESSING.value)
        .values(poster_status=PosterStatus.PENDING.value, poster_locked_until=None)
    )
    await session.commit()
    return result.rowcount


async def requeue_poster(session: AsyncSession, media: MediaFile) -> None:
    """Admin regenerate: reset attempts and queue again (caller commits)"""
    media.poster_status = PosterStatus.PENDING.value
    media.poster_attempts = 0
    media.poster_locked_until = None
    media.poster_worker = None
    media.poster_error = None
    media.poster_generated_at = None


async def reconcile_posters(session: AsyncSession, storage_root: Path, retry_failed: bool = False) -> Dict[str, int]:
    """Repair poster state against the filesystem.

    - expired claims on their last attempt become failed (fail_exhausted_posters, first)
    - video rows without a status (legacy uploads) become pending
    - ready rows whose poster file is missing become pending
    - failed rows become pending with attempts reset when retry_failed is true
    """
    counts = {"failed_abandoned": 0, "queued_untracked": 0, "requeued_missing": 0, "retried_failed": 0}
    counts["failed_abandoned"] = await fail_exhausted_posters(session)
    videos = (await session.exec(
        select(MediaFile)
        .where(MediaFile.is_deleted == False, MediaFile.media_type == MediaType.VIDEO.value)
        .execution_options(populate_existing=True)
    )).all()
    for media in videos:
        if media.poster_status is None:
            await requeue_poster(session, media)
            counts["queued_untracked"] += 1
        elif media.poster_status == PosterStatus.READY.value:
            poster = media.resolve_poster_path(storage_root)
            if poster is None or not poster.exists():
                await requeue_poster(session, media)
                counts["requeued_missing"] += 1
        elif media.poster_status == PosterStatus.FAILED.value and retry_failed:
            await requeue_poster(session, media)
            counts["retried_failed"] += 1
    await session.commit()
    return counts


async def poster_counts(session: AsyncSession, post_ids: Optional[Iterable[int]] = None) -> Dict[str, int]:
    """Videos by poster status; post_ids limits the count to media of observations of those posts"""
    query = (
        select(MediaFile.poster_status, func.count())
        .where(MediaFile.is_deleted == False, MediaFile.media_type == MediaType.VIDEO.value)
        .group_by(MediaFile.poster_status)
    )
    if post_ids is not None:
        query = query.join(Observation, Observation.id == MediaFile.observation_id).where(Observation.post_id.in_(list(post_ids)))
    rows = (await session.exec(query)).all()
    counts = {status.value: 0 for status in PosterStatus}
    counts["untracked"] = 0
    for status_value, count in rows:
        counts[status_value or "untracked"] = int(count)
    return counts


async def heartbeat(
    session: AsyncSession,
    name: str,
    kind: str,
    hostname: str,
    pid: int,
    started_at: datetime,
    jobs_done: int,
    jobs_failed: int,
    details: Optional[str] = None,
) -> None:
    row = await session.get(WorkerHeartbeat, name)
    if row is None:
        row = WorkerHeartbeat(name=name, kind=kind, hostname=hostname, pid=pid, started_at=started_at)
        session.add(row)
    row.last_seen_at = utcnow()
    row.jobs_done = jobs_done
    row.jobs_failed = jobs_failed
    row.details = details
    await session.commit()


async def remove_heartbeat(session: AsyncSession, name: str) -> None:
    row = await session.get(WorkerHeartbeat, name)
    if row is not None:
        await session.delete(row)
        await session.commit()


def is_stale(last_seen_at: Optional[datetime], now: Optional[datetime] = None) -> bool:
    if last_seen_at is None:
        return True
    now = now or utcnow()
    return (now - _as_aware(last_seen_at)) > timedelta(seconds=settings.storage.poster.stale_after_seconds)


async def worker_heartbeats(session: AsyncSession) -> List[WorkerHeartbeat]:
    return list((await session.exec(select(WorkerHeartbeat).order_by(WorkerHeartbeat.name))).all())
