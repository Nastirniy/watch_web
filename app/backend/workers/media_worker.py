"""Media worker: extracts video posters queued on media_files.

Runs as its own process (a second container from the same image, or a plain
systemd unit). It polls the database for claimable video rows, runs ffmpeg in
a thread with a hard timeout, writes the poster atomically and records the
outcome. Image thumbnails never come through here: imgproxy renders them on
demand.

    python -m app.backend.workers.media_worker

Stops cleanly on SIGTERM / SIGINT: running jobs finish, unstarted claims are
released, the heartbeat row is removed.
"""
from __future__ import annotations

import asyncio
import os
import signal
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional

from app.backend.config import settings
from app.backend.models.media import MediaFile
from app.backend.services.media import jobs, uploads
from app.backend.services.media.posters import PosterError, make_poster, resolve_binary
from app.backend.services.media.probe import metadata_for, resolve_tools
from app.backend.utils.logging_config import get_logger

_logger = get_logger("media_worker")

WORKER_KIND = "media"

# Signature of the poster extractor so tests can inject a fake:
# (source, target) -> None, raising PosterError on failure
Extractor = Callable[[Path, Path], None]


class MediaWorker:
    def __init__(
        self,
        session_maker,
        storage_root: Path,
        name: Optional[str] = None,
        extractor: Optional[Extractor] = None,
        concurrency: Optional[int] = None,
    ):
        self.session_maker = session_maker
        self.storage_root = storage_root
        self.hostname = socket.gethostname()
        self.pid = os.getpid()
        self.name = name or f"{WORKER_KIND}@{self.hostname}:{self.pid}"
        self.extractor = extractor or self._default_extractor()
        self.concurrency = concurrency or settings.storage.poster.concurrency
        self.started_at = datetime.now(timezone.utc)
        self.jobs_done = 0
        self.jobs_failed = 0
        self._stop = asyncio.Event()
        self._running: set[asyncio.Task] = set()

    # ---- setup ---------------------------------------------------------

    @staticmethod
    def _default_extractor() -> Extractor:
        cfg = settings.storage
        ffmpeg = resolve_binary(cfg.ffmpeg_path, imageio_fallback=True)
        ffprobe = resolve_binary(cfg.ffprobe_path)
        if not ffmpeg:
            raise RuntimeError(
                f"ffmpeg not found (storage.ffmpeg_path={cfg.ffmpeg_path!r}); install ffmpeg or the imageio-ffmpeg package"
            )
        if not ffprobe:
            _logger.warning("ffprobe not found; durations will be read from ffmpeg output instead")
        _logger.info(f"ffmpeg: {ffmpeg}; ffprobe: {ffprobe or 'not available'}")

        def extract(source: Path, target: Path) -> None:
            result = make_poster(source, target, cfg.poster, ffmpeg, ffprobe)
            _logger.debug(f"poster {target.name}: seek={result.seek_seconds:.1f}s duration={result.duration_seconds}")

        return extract

    def request_stop(self) -> None:
        if not self._stop.is_set():
            _logger.info("stop requested; finishing running jobs")
            self._stop.set()

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
            await asyncio.to_thread(self.extractor, source, target)
            metadata = None
            if media.duration_seconds is None or media.playback_type is None:
                metadata = await asyncio.to_thread(self._probe, source, media.mime_type)
            async with self.session_maker() as session:
                owned = await jobs.complete_poster(session, media.id, self.name, metadata=metadata or None)
            if not owned:
                _logger.warning(f"poster job media={media.id}: lease lost before completion, result kept on disk")
                return False
            self.jobs_done += 1
            _logger.info(f"poster ready media={media.id} -> {target.relative_to(self.storage_root).as_posix()}")
            return True
        except Exception as exc:  # PosterError, OSError, anything from ffmpeg
            message = f"{type(exc).__name__}: {exc}"
            async with self.session_maker() as session:
                new_status = await jobs.fail_poster(session, media.id, self.name, message)
            self.jobs_failed += 1
            _logger.error(f"poster job media={media.id} failed ({new_status or 'lease lost'}): {message}")
            return False
        finally:
            renew.cancel()

    def _probe(self, source: Path, mime_type: str) -> dict:
        """Codec and duration metadata; empty when ffprobe cannot read the file (never fails the poster job)"""
        try:
            ffmpeg, ffprobe = resolve_tools()
            return metadata_for(source, mime_type, ffmpeg, ffprobe)
        except Exception as exc:
            _logger.warning(f"probe failed for {source.name}: {exc}")
            return {}

    async def _renew_lease_loop(self, media_id: int) -> None:
        interval = max(5, settings.storage.poster.lease_seconds // 2)
        try:
            while True:
                await asyncio.sleep(interval)
                async with self.session_maker() as session:
                    if not await jobs.renew_lease(session, media_id, self.name):
                        _logger.warning(f"poster job media={media_id}: could not renew lease")
                        return
        except asyncio.CancelledError:
            pass

    async def claim_and_run_once(self) -> bool:
        """Claim one job and process it. False when the queue is empty."""
        async with self.session_maker() as session:
            media = await jobs.claim_next_poster(session, self.name)
        if media is None:
            return False
        await self.process(media)
        return True

    # ---- loops ---------------------------------------------------------

    async def _sweep_loop(self) -> None:
        """Expire abandoned resumable uploads and remove their part files"""
        interval = settings.storage.upload.sweep_interval_seconds
        while not self._stop.is_set():
            try:
                async with self.session_maker() as session:
                    counts = await uploads.expire_sessions(session, self.storage_root)
                if counts["expired"] or counts["cleaned"]:
                    _logger.info(f"upload sessions sweep: {counts}")
            except Exception as exc:
                _logger.warning(f"upload sessions sweep failed: {exc}")
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
                        details=f"running={len(self._running)} concurrency={self.concurrency}",
                    )
            except Exception as exc:
                _logger.warning(f"heartbeat failed: {exc}")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def run(self) -> None:
        cfg = settings.storage.poster
        _logger.info(f"media worker {self.name} starting; storage={self.storage_root}; concurrency={self.concurrency}")
        async with self.session_maker() as session:
            counts = await jobs.reconcile_posters(session, self.storage_root)
        _logger.info(f"reconcile on start: {counts}")

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
                async with self.session_maker() as session:
                    media = await jobs.claim_next_poster(session, self.name)
                if media is None:
                    semaphore.release()
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=idle_delay)
                    except asyncio.TimeoutError:
                        pass
                    idle_delay = min(idle_delay * 1.5, cfg.idle_poll_interval_seconds)
                    continue
                idle_delay = cfg.poll_interval_seconds
                task = asyncio.create_task(self._run_and_release(media, semaphore))
                self._running.add(task)
                task.add_done_callback(self._running.discard)
        finally:
            if self._running:
                _logger.info(f"waiting for {len(self._running)} running job(s)")
                await asyncio.gather(*self._running, return_exceptions=True)
            heartbeat_task.cancel()
            sweep_task.cancel()
            async with self.session_maker() as session:
                released = await jobs.release_claims(session, self.name)
                await jobs.remove_heartbeat(session, self.name)
            _logger.info(f"media worker {self.name} stopped; released={released} done={self.jobs_done} failed={self.jobs_failed}")

    async def _run_and_release(self, media: MediaFile, semaphore: asyncio.Semaphore) -> None:
        try:
            await self.process(media)
        finally:
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
    worker = MediaWorker(session_maker, storage_root)
    _install_signal_handlers(asyncio.get_running_loop(), worker)
    try:
        await worker.run()
    finally:
        await close_db()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(0)
