"""Media worker: extracts video posters and storyboards.

Runs as its own process (a second container from the same image, or a plain
systemd unit). It polls the database for claimable jobs, runs ffmpeg in a
thread with a hard timeout, writes the result atomically and records the
outcome. Image thumbnails never come through here: imgproxy renders them on
demand.

Two queues share the `poster.concurrency` places. Posters (media_files rows)
come first on every loop turn; a storyboard (video_storyboards rows) is claimed
only when no poster was, and at most `storyboard.slots` run at once. Slots stay
below the concurrency, so a poster always finds a free place: a storyboard
never delays a poster.

    python -m app.backend.workers.media_worker

Stops cleanly on SIGTERM / SIGINT: running posters finish, running storyboards
are stopped and given back to the queue (a storyboard may take far longer than
the stop grace period), unstarted claims are released, the heartbeat row is
removed.
"""
from __future__ import annotations

import asyncio
import os
import signal
import socket
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Dict, Optional

from app.backend.config import settings
from app.backend.models.media import MediaFile
from app.backend.models.storyboard import VideoStoryboard, storyboard_relative_dir
from app.backend.services.media import jobs, storyboard_jobs, uploads
from app.backend.services.media.posters import PosterError, make_poster, resolve_binary
from app.backend.services.media.probe import has_no_video_stream, metadata_for, resolve_tools
from app.backend.services.media.storyboards import (
    StoryboardCancelled,
    StoryboardError,
    StoryboardPlan,
    StoryboardResult,
    extract_storyboard,
    plan_storyboard,
)
from app.backend.utils.logging_config import get_logger

_logger = get_logger("media_worker")

WORKER_KIND = "media"
# A running job renews its lease every half lease, never more often than this
LEASE_RENEW_MIN_SECONDS = 5

# Signature of the poster extractor so tests can inject a fake:
# (source, target) -> None, raising PosterError on failure
Extractor = Callable[[Path, Path], None]

# Signature of the storyboard extractor so tests can inject a fake:
# (source, target_dir, plan, cancel) -> StoryboardResult, raising StoryboardError on failure
# and StoryboardCancelled once cancel is set (the worker is stopping)
StoryboardExtractor = Callable[[Path, Path, StoryboardPlan, threading.Event], StoryboardResult]


class MediaWorker:
    def __init__(
        self,
        session_maker,
        storage_root: Path,
        name: Optional[str] = None,
        extractor: Optional[Extractor] = None,
        concurrency: Optional[int] = None,
        storyboard_extractor: Optional[StoryboardExtractor] = None,
    ):
        self.concurrency = concurrency or settings.storage.poster.concurrency
        storyboard = settings.storage.storyboard
        if storyboard.enabled and storyboard.slots >= self.concurrency:
            raise ValueError(
                f"storage.storyboard.slots ({storyboard.slots}) must be below storage.poster.concurrency "
                f"({self.concurrency}) so a poster always finds a free place: raise the concurrency, "
                f"lower the slots or set storage.storyboard.enabled = false"
            )
        self.session_maker = session_maker
        self.storage_root = storage_root
        self.hostname = socket.gethostname()
        self.pid = os.getpid()
        self.name = name or f"{WORKER_KIND}@{self.hostname}:{self.pid}"
        self.extractor = extractor or self._default_extractor()
        self.storyboard_extractor = storyboard_extractor or self._default_storyboard_extractor()
        self.started_at = datetime.now(timezone.utc)
        self.jobs_done = 0
        self.jobs_failed = 0
        self._stop = asyncio.Event()
        self._running: set[asyncio.Task] = set()
        # Storyboard jobs among _running; counted when claimed, so the slots limit is exact
        self._storyboards_running = 0
        # Cancel events of the running storyboards, set on stop
        self._storyboard_cancels: set[threading.Event] = set()

    # ---- setup ---------------------------------------------------------

    @staticmethod
    def _require_ffmpeg() -> str:
        configured = settings.storage.ffmpeg_path
        ffmpeg = resolve_binary(configured, imageio_fallback=True)
        if not ffmpeg:
            raise RuntimeError(
                f"ffmpeg not found (storage.ffmpeg_path={configured!r}); install ffmpeg or the imageio-ffmpeg package"
            )
        return ffmpeg

    @staticmethod
    def _default_storyboard_extractor() -> StoryboardExtractor:
        cfg = settings.storage
        ffmpeg = MediaWorker._require_ffmpeg()

        def extract(source: Path, target_dir: Path, plan: StoryboardPlan, cancel: threading.Event) -> StoryboardResult:
            return extract_storyboard(source, target_dir, plan, cfg.storyboard, ffmpeg, cancel=cancel)

        return extract

    @staticmethod
    def _default_extractor() -> Extractor:
        cfg = settings.storage
        ffmpeg = MediaWorker._require_ffmpeg()
        ffprobe = resolve_binary(cfg.ffprobe_path)
        if not ffprobe:
            _logger.warning("ffprobe not found; durations will be read from ffmpeg output instead")
        _logger.info(f"ffmpeg: {ffmpeg}; ffprobe: {ffprobe or 'not available'}")

        def extract(source: Path, target: Path) -> None:
            result = make_poster(source, target, cfg.poster, ffmpeg, ffprobe)
            _logger.debug(f"poster {target.name}: seek={result.seek_seconds:.1f}s duration={result.duration_seconds}")

        return extract

    def request_stop(self) -> None:
        if not self._stop.is_set():
            _logger.info("stop requested; finishing running posters, stopping running storyboards")
            self._stop.set()
        self._cancel_storyboards()

    def _cancel_storyboards(self) -> None:
        for cancel in self._storyboard_cancels:
            cancel.set()

    # ---- job processing ------------------------------------------------

    async def process(self, media: MediaFile) -> bool:
        """Extract the poster for one claimed row and record the outcome. True on success."""
        source = media.resolve_path(self.storage_root)
        target = media.resolve_poster_path(self.storage_root)
        renew = asyncio.create_task(self._renew_lease_loop(media.id))
        try:
            if target is None:
                raise PosterError("row is not a video")
            _logger.info(f"poster job media={media.id} attempt={media.poster_attempts} source={source.name}")
            metadata = None
            if media.duration_seconds is None or media.playback_type is None:
                metadata = await asyncio.to_thread(self._probe, source, media.mime_type)
            if has_no_video_stream({"video_codec": media.video_codec, "audio_codec": media.audio_codec, **(metadata or {})}):
                # Only audio in the file: no frame to extract, now or after any number of retries
                async with self.session_maker() as session:
                    owned = await jobs.mark_poster_unsupported(session, media.id, worker=self.name, metadata=metadata or None)
                _logger.info(f"poster job media={media.id}: no video stream, unsupported{'' if owned else ' (lease lost)'}")
                return False
            await asyncio.to_thread(self.extractor, source, target)
            async with self.session_maker() as session:
                owned = await jobs.complete_poster(session, media.id, self.name, metadata=metadata or None)
            if not owned:
                _logger.warning(f"poster job media={media.id}: lease lost before completion, result kept on disk")
                return False
            self.jobs_done += 1
            _logger.info(f"poster ready media={media.id} -> {target.relative_to(self.storage_root).as_posix()}")
        except Exception as exc:  # PosterError, OSError, anything from ffmpeg
            message = f"{type(exc).__name__}: {exc}"
            async with self.session_maker() as session:
                new_status = await jobs.fail_poster(session, media.id, self.name, message)
            self.jobs_failed += 1
            _logger.error(f"poster job media={media.id} failed ({new_status or 'lease lost'}): {message}")
            return False
        finally:
            renew.cancel()
        await self._enqueue_storyboard(media.id)
        return True

    async def _enqueue_storyboard(self, media_id: int) -> None:
        """Queue the storyboard of a video whose poster just became ready (its duration is known then).

        The row is read again in a session of its own: the poster job may have just stored the
        probed duration, and enqueue_storyboard commits (or rolls back) the session it is given.
        """
        try:
            async with self.session_maker() as session:
                media = await session.get(MediaFile, media_id)
                if media is not None and await storyboard_jobs.enqueue_storyboard(session, media):
                    _logger.info(f"storyboard queued media={media_id}")
        except Exception as exc:  # worker loop: the poster is done either way; reconciliation on start queues it
            _logger.error(f"storyboard enqueue media={media_id} failed: {type(exc).__name__}: {exc}")

    async def process_storyboard(self, row: VideoStoryboard) -> bool:
        """Render the storyboard of one claimed row and record the outcome. True on success.

        A stop cancels it: ffmpeg is stopped and the row goes back to the queue without a failure.
        """
        media_id = row.media_id
        cancel = threading.Event()
        if self._stop.is_set():
            cancel.set()
        self._storyboard_cancels.add(cancel)
        renew = asyncio.create_task(self._renew_storyboard_lease_loop(media_id))
        try:
            async with self.session_maker() as session:
                media = await session.get(MediaFile, media_id)
            if media is None or media.duration_seconds is None:
                raise StoryboardError("video row or its duration is missing")
            plan = plan_storyboard(media.duration_seconds, settings.storage.storyboard)
            source = media.resolve_path(self.storage_root)
            target_dir = self.storage_root / storyboard_relative_dir(media_id)
            _logger.info(
                f"storyboard job media={media_id} attempt={row.attempts} step={plan.step_seconds}s "
                f"frames={plan.frame_count} sheets={plan.sheet_count} source={source.name}"
            )
            if cancel.is_set():
                raise StoryboardCancelled("the worker is stopping")
            result = await asyncio.to_thread(self.storyboard_extractor, source, target_dir, plan, cancel)
            async with self.session_maker() as session:
                owned = await storyboard_jobs.complete_storyboard(session, media_id, self.name, plan, result)
            if not owned:
                _logger.warning(f"storyboard job media={media_id}: lease lost before completion, result kept on disk")
                return False
            self.jobs_done += 1
            _logger.info(
                f"storyboard ready media={media_id} -> {storyboard_relative_dir(media_id)} sheets={result.sheet_count}"
            )
            return True
        except StoryboardCancelled:
            return await self._give_back_storyboard(media_id)
        except Exception as exc:  # StoryboardError, OSError, anything from ffmpeg
            if cancel.is_set():
                # A process-group stop signal (systemd) reaches ffmpeg too, which may fail just before
                # the cancel: a run ended by a stop is never a failure of the video
                return await self._give_back_storyboard(media_id)
            message = f"{type(exc).__name__}: {exc}"
            async with self.session_maker() as session:
                new_status = await storyboard_jobs.fail_storyboard(session, media_id, self.name, message)
            self.jobs_failed += 1
            _logger.error(f"storyboard job media={media_id} failed ({new_status or 'lease lost'}): {message}")
            return False
        finally:
            renew.cancel()
            self._storyboard_cancels.discard(cancel)

    async def _give_back_storyboard(self, media_id: int) -> bool:
        """Return a storyboard stopped by a shutdown to the queue, the attempt not counted. Always False."""
        async with self.session_maker() as session:
            released = await storyboard_jobs.release_storyboard(session, media_id, self.name)
        outcome = "back in the queue" if released else "lease lost"
        _logger.info(f"storyboard job media={media_id} stopped by shutdown; {outcome}")
        return False

    def _probe(self, source: Path, mime_type: str) -> dict:
        """Codec and duration metadata; empty when ffprobe cannot read the file (never fails the poster job)"""
        try:
            ffmpeg, ffprobe = resolve_tools()
            return metadata_for(source, mime_type, ffmpeg, ffprobe)
        except Exception as exc:
            _logger.warning(f"probe failed for {source.name}: {exc}")
            return {}

    async def _renew_lease_loop(self, media_id: int) -> None:
        await self._keep_lease(media_id, "poster", jobs.renew_lease, settings.storage.poster.lease_seconds)

    async def _renew_storyboard_lease_loop(self, media_id: int) -> None:
        await self._keep_lease(
            media_id, "storyboard", storyboard_jobs.renew_storyboard_lease, settings.storage.storyboard.lease_seconds
        )

    async def _keep_lease(
        self, media_id: int, kind: str, renew: Callable[..., Awaitable[bool]], lease_seconds: int
    ) -> None:
        """Renew the claim of a running job every half lease until cancelled or the lease is lost"""
        interval = max(LEASE_RENEW_MIN_SECONDS, lease_seconds // 2)
        try:
            while True:
                await asyncio.sleep(interval)
                async with self.session_maker() as session:
                    if not await renew(session, media_id, self.name):
                        _logger.warning(f"{kind} job media={media_id}: could not renew lease")
                        return
        except asyncio.CancelledError:
            pass

    async def claim_and_run_once(self) -> bool:
        """Claim one job and process it. False when the queue is empty."""
        media = await self._claim_poster()
        if media is None:
            return False
        await self.process(media)
        return True

    async def _claim_poster(self) -> Optional[MediaFile]:
        """Fail the claims whose last attempt took their worker down, then claim the next poster job"""
        async with self.session_maker() as session:
            failed = await jobs.fail_exhausted_posters(session)
            if failed:
                _logger.error(f"{failed} poster job(s) failed: {jobs.WORKER_STOPPED_ERROR}")
            return await jobs.claim_next_poster(session, self.name)

    async def _claim_storyboard(self) -> Optional[VideoStoryboard]:
        """Fail the claims whose last attempt took their worker down, then claim the next storyboard job"""
        async with self.session_maker() as session:
            failed = await storyboard_jobs.fail_exhausted_storyboards(session)
            if failed:
                _logger.error(f"{failed} storyboard job(s) failed: {jobs.WORKER_STOPPED_ERROR}")
            return await storyboard_jobs.claim_next_storyboard(session, self.name)

    # ---- loops ---------------------------------------------------------

    async def _sweep_loop(self) -> None:
        """Every sweep interval: expire abandoned resumable uploads and remove their part files, and queue the
        storyboards of videos that became eligible while the worker runs (a duration filled in by probe-missing, an
        enqueue that failed); database only, the filesystem part of the reconciliation runs at start"""
        interval = settings.storage.upload.sweep_interval_seconds
        while not self._stop.is_set():
            try:
                async with self.session_maker() as session:
                    counts = await uploads.expire_sessions(session, self.storage_root)
                if counts["expired"] or counts["cleaned"]:
                    _logger.info(f"upload sessions sweep: {counts}")
            except Exception as exc:
                _logger.warning(f"upload sessions sweep failed: {exc}")
            if settings.storage.storyboard.enabled:
                try:
                    async with self.session_maker() as session:
                        queued = await storyboard_jobs.queue_missing_storyboards(session)
                    if queued:
                        _logger.info(f"storyboard sweep: queued {queued} video(s) without a row")
                except Exception as exc:  # worker loop: the next sweep tries again
                    _logger.warning(f"storyboard sweep failed: {exc}")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _heartbeat_loop(self) -> None:
        interval = settings.storage.poster.heartbeat_seconds
        while not self._stop.is_set():
            try:
                async with self.session_maker() as session:
                    await jobs.heartbeat(
                        session, self.name, WORKER_KIND, self.hostname, self.pid,
                        self.started_at, self.jobs_done, self.jobs_failed,
                        details=(
                            f"running={len(self._running)} storyboards={self._storyboards_running} "
                            f"concurrency={self.concurrency}"
                        ),
                    )
            except Exception as exc:
                _logger.warning(f"heartbeat failed: {exc}")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _reconcile_storyboards_on_start(self) -> Optional[Dict[str, int]]:
        """Storyboard reconciliation at start; None when the filesystem under _derived/storyboards failed it.

        That OSError (an unreadable directory, a wrong owner) is logged and the worker goes on: posters never
        depend on storyboards, and the sweep loop queues missing rows without the filesystem.
        """
        try:
            async with self.session_maker() as session:
                return await storyboard_jobs.reconcile_storyboards(session, self.storage_root)
        except OSError as exc:
            _logger.error(
                f"storyboard reconcile on start failed, the worker goes on without it: {type(exc).__name__}: {exc}"
            )
            return None

    def _storyboard_slot_free(self) -> bool:
        config = settings.storage.storyboard
        return config.enabled and self._storyboards_running < config.slots

    async def run(self) -> None:
        cfg = settings.storage.poster
        storyboard = settings.storage.storyboard
        _logger.info(
            f"media worker {self.name} starting; storage={self.storage_root}; concurrency={self.concurrency}; "
            f"storyboard slots={storyboard.slots if storyboard.enabled else 'off'}"
        )
        async with self.session_maker() as session:
            counts = await jobs.reconcile_posters(session, self.storage_root)
        storyboard_counts = await self._reconcile_storyboards_on_start()
        _logger.info(f"reconcile on start: posters={counts} storyboards={storyboard_counts}")

        heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        sweep_task = asyncio.create_task(self._sweep_loop())
        semaphore = asyncio.Semaphore(self.concurrency)
        idle_delay = cfg.poll_interval_seconds
        try:
            while not self._stop.is_set():
                await semaphore.acquire()
                if self._stop.is_set():
                    semaphore.release()
                    break
                # A poster first; a storyboard only when no poster is waiting and a slot is free
                media = await self._claim_poster()
                row = None
                if media is None and self._storyboard_slot_free():
                    row = await self._claim_storyboard()
                if media is None and row is None:
                    semaphore.release()
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=idle_delay)
                    except asyncio.TimeoutError:
                        pass
                    idle_delay = min(idle_delay * 1.5, cfg.idle_poll_interval_seconds)
                    continue
                idle_delay = cfg.poll_interval_seconds
                if media is not None:
                    task = asyncio.create_task(self._run_and_release(media, semaphore))
                else:
                    self._storyboards_running += 1
                    task = asyncio.create_task(self._run_storyboard_and_release(row, semaphore))
                self._running.add(task)
                task.add_done_callback(self._running.discard)
        finally:
            # Posters finish; storyboards stop now (also when the loop ends without request_stop,
            # and a storyboard that registers its cancel event later sees _stop at once)
            self._stop.set()
            self._cancel_storyboards()
            if self._running:
                _logger.info(f"waiting for {len(self._running)} running job(s)")
                await asyncio.gather(*self._running, return_exceptions=True)
            heartbeat_task.cancel()
            sweep_task.cancel()
            async with self.session_maker() as session:
                released = await jobs.release_claims(session, self.name)
                released_storyboards = await storyboard_jobs.release_storyboard_claims(session, self.name)
                await jobs.remove_heartbeat(session, self.name)
            _logger.info(
                f"media worker {self.name} stopped; released={released} released_storyboards={released_storyboards} "
                f"done={self.jobs_done} failed={self.jobs_failed}"
            )

    async def _run_and_release(self, media: MediaFile, semaphore: asyncio.Semaphore) -> None:
        try:
            await self.process(media)
        finally:
            semaphore.release()

    async def _run_storyboard_and_release(self, row: VideoStoryboard, semaphore: asyncio.Semaphore) -> None:
        try:
            await self.process_storyboard(row)
        finally:
            self._storyboards_running -= 1
            semaphore.release()


def _install_signal_handlers(loop: asyncio.AbstractEventLoop, worker: MediaWorker) -> None:
    for sig in (getattr(signal, "SIGTERM", None), getattr(signal, "SIGINT", None)):
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, worker.request_stop)
        except (NotImplementedError, RuntimeError):
            # Windows: add_signal_handler is unavailable; fall back to the signal module
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(worker.request_stop))


async def main() -> int:
    from app.backend.database import get_session_maker, close_db

    storage_root = Path(settings.storage.upload_path)
    if not storage_root.is_absolute():
        storage_root = storage_root.resolve()
    storage_root.mkdir(parents=True, exist_ok=True)

    session_maker = await get_session_maker()
    try:
        # Raises on a configuration error (ffmpeg missing, storyboard slots not below the concurrency)
        worker = MediaWorker(session_maker, storage_root)
        _install_signal_handlers(asyncio.get_running_loop(), worker)
        await worker.run()
    finally:
        await close_db()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)
