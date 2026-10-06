#!/usr/bin/env python3
"""
Media maintenance commands.

Usage:
    python -m app.backend.media_tools cleanup-legacy-thumbnails [--dry-run]
    python -m app.backend.media_tools reconcile-posters [--retry-failed]
    python -m app.backend.media_tools poster-status
    python -m app.backend.media_tools reconcile-storyboards [--retry-failed]
    python -m app.backend.media_tools storyboard-status
    python -m app.backend.media_tools probe-missing [--limit N]
    python -m app.backend.media_tools expire-uploads

cleanup-legacy-thumbnails removes the *_thumb_small|medium|large.jpg files the
old pipeline wrote next to the originals. Thumbnails are now rendered on
demand, so these files are dead weight.
"""
import argparse
import asyncio
import re
import sys
from pathlib import Path

from app.backend.config import settings

LEGACY_THUMB_RE = re.compile(r"_thumb_(small|medium|large)\.jpg$", re.IGNORECASE)


def _storage_root() -> Path:
    root = Path(settings.storage.upload_path)
    return root if root.is_absolute() else root.resolve()


def cleanup_legacy_thumbnails(dry_run: bool) -> int:
    root = _storage_root()
    removed = 0
    freed = 0
    for path in root.rglob("*_thumb_*.jpg"):
        if not LEGACY_THUMB_RE.search(path.name) or not path.is_file():
            continue
        size = path.stat().st_size
        print(f"{'would remove' if dry_run else 'removing'} {path.relative_to(root).as_posix()} ({size} bytes)")
        if not dry_run:
            path.unlink()
        removed += 1
        freed += size
    print(f"{'would remove' if dry_run else 'removed'} {removed} file(s), {freed / (1024 * 1024):.1f} MB")
    return 0


async def reconcile_posters(retry_failed: bool) -> int:
    from app.backend.database import get_session_maker, close_db
    from app.backend.services.media.jobs import reconcile_posters as reconcile

    session_maker = await get_session_maker()
    try:
        async with session_maker() as session:
            counts = await reconcile(session, _storage_root(), retry_failed=retry_failed)
        for key, value in counts.items():
            print(f"{key}: {value}")
    finally:
        await close_db()
    return 0


async def poster_status() -> int:
    from app.backend.database import get_session_maker, close_db
    from app.backend.services.media.jobs import poster_counts, worker_heartbeats, is_stale

    session_maker = await get_session_maker()
    try:
        async with session_maker() as session:
            counts = await poster_counts(session)
            workers = await worker_heartbeats(session)
        print("posters:")
        for key, value in counts.items():
            print(f"  {key}: {value}")
        print("workers:")
        if not workers:
            print("  none registered")
        for worker in workers:
            state = "stale" if is_stale(worker.last_seen_at) else "alive"
            print(f"  {worker.name} {state} last_seen={worker.last_seen_at} done={worker.jobs_done} failed={worker.jobs_failed}")
    finally:
        await close_db()
    return 0


async def reconcile_storyboards_command(retry_failed: bool) -> int:
    from app.backend.database import get_session_maker, close_db
    from app.backend.services.media.storyboard_jobs import reconcile_storyboards as reconcile

    session_maker = await get_session_maker()
    try:
        async with session_maker() as session:
            counts = await reconcile(session, _storage_root(), retry_failed=retry_failed)
        for key, value in counts.items():
            print(f"{key}: {value}")
    finally:
        await close_db()
    return 0


async def storyboard_status() -> int:
    from app.backend.database import get_session_maker, close_db
    from app.backend.services.media.storyboard_jobs import storyboard_counts, latest_storyboard_errors

    session_maker = await get_session_maker()
    try:
        async with session_maker() as session:
            counts = await storyboard_counts(session)
            errors = await latest_storyboard_errors(session)
        print("storyboards:")
        for key, value in counts.items():
            print(f"  {key}: {value}")
        if errors:
            print("latest errors:")
            for media_id, status_value, attempts, error in errors:
                print(f"  media {media_id} {status_value} attempts={attempts}: {error}")
    finally:
        await close_db()
    return 0


async def probe_missing(limit: int) -> int:
    """Fill duration/codec/playback_type for video and audio rows that have none (existing uploads)"""
    from sqlmodel import select
    from app.backend.database import get_session_maker, close_db
    from app.backend.models.media import MediaFile
    from app.backend.services.media.probe import metadata_for, resolve_tools

    ffmpeg, ffprobe = resolve_tools()
    if not ffmpeg and not ffprobe:
        print("neither ffprobe nor ffmpeg is available")
        return 1
    root = _storage_root()
    session_maker = await get_session_maker()
    done = failed = 0
    try:
        async with session_maker() as session:
            rows = (await session.exec(
                select(MediaFile)
                .where(MediaFile.is_deleted == False, MediaFile.media_type.in_(("video", "audio")), MediaFile.playback_type.is_(None))
                .order_by(MediaFile.id).limit(limit)
            )).all()
            for row in rows:
                values = metadata_for(row.resolve_path(root), row.mime_type, ffmpeg, ffprobe)
                if not values:
                    failed += 1
                    print(f"media {row.id}: probe failed ({row.stored_filename})")
                    continue
                for key, value in values.items():
                    setattr(row, key, value)
                done += 1
                print(f"media {row.id}: {values.get('playback_type')} duration={values.get('duration_seconds')}")
            await session.commit()
    finally:
        await close_db()
    print(f"probed {done} file(s), {failed} failed")
    return 0


async def expire_uploads() -> int:
    """Expire abandoned resumable upload sessions and delete their part files (the worker does this too)"""
    from app.backend.database import get_session_maker, close_db
    from app.backend.services.media.uploads import expire_sessions

    session_maker = await get_session_maker()
    try:
        async with session_maker() as session:
            counts = await expire_sessions(session, _storage_root())
        for key, value in counts.items():
            print(f"{key}: {value}")
    finally:
        await close_db()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Media maintenance commands")
    sub = parser.add_subparsers(dest="command", required=True)

    cleanup = sub.add_parser("cleanup-legacy-thumbnails", help="delete *_thumb_*.jpg files written by the old pipeline")
    cleanup.add_argument("--dry-run", action="store_true", help="list files without deleting them")

    reconcile = sub.add_parser("reconcile-posters", help="repair poster state against the filesystem")
    reconcile.add_argument("--retry-failed", action="store_true", help="also re-queue videos whose extraction failed")

    sub.add_parser("poster-status", help="queue depth and worker heartbeats")

    reconcile_sb = sub.add_parser("reconcile-storyboards", help="repair storyboard state against the filesystem")
    reconcile_sb.add_argument("--retry-failed", action="store_true", help="also re-queue videos whose storyboard generation failed")

    sub.add_parser("storyboard-status", help="storyboard jobs by status")

    probe = sub.add_parser("probe-missing", help="fill duration, codecs and playback_type for existing video and audio rows")
    probe.add_argument("--limit", type=int, default=1000)

    sub.add_parser("expire-uploads", help="expire abandoned resumable upload sessions")

    args = parser.parse_args(argv)
    if args.command == "cleanup-legacy-thumbnails":
        return cleanup_legacy_thumbnails(args.dry_run)
    if args.command == "reconcile-posters":
        return asyncio.run(reconcile_posters(args.retry_failed))
    if args.command == "poster-status":
        return asyncio.run(poster_status())
    if args.command == "reconcile-storyboards":
        return asyncio.run(reconcile_storyboards_command(args.retry_failed))
    if args.command == "storyboard-status":
        return asyncio.run(storyboard_status())
    if args.command == "probe-missing":
        return asyncio.run(probe_missing(args.limit))
    if args.command == "expire-uploads":
        return asyncio.run(expire_uploads())
    return 1


if __name__ == "__main__":
    sys.exit(main())
