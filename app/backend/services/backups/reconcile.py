"""After a restore: the media directory and the PKI store against the restored rows (spec section 8.3).

A rollback restores rows, not files. Files of rows the restore took away (uploads, certificate packages made after
the backup) are moved aside, never deleted: media to <upload_path>/_orphaned/<time>/<same path>, packages (private
keys) to <store_dir>/.quarantine/<time>/<user_id>/<certificate_id>/. Derived files (posters, storyboards) of ids
without a row are deleted: they are rebuilt from the original when needed. A row whose original sits under
_orphaned (an undo after an apply) gets it back. Nothing younger than MIN_AGE_SECONDS is moved: an upload or an issue
may be on its way.
"""
import os
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Optional

from loguru import logger
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.models.media import POSTER_DIR, MediaFile
from app.backend.models.storyboard import STORYBOARD_DIR
from app.backend.models.user_certificate import UserCertificate

ORPHANED_DIR = "_orphaned"
QUARANTINE_DIR = ".quarantine"
SKIPPED_TOP = frozenset({"_derived", "_tmp", ORPHANED_DIR})
# Never ours at any level: hidden files and in-flight copies (.tmp-...), NAS snapshot / index / recycle folders
SKIPPED_PREFIXES = (".", "@", "#")
SKIPPED_NAMES = frozenset({"lost+found"})


def _ours(name: str) -> bool:
    return not name.startswith(SKIPPED_PREFIXES) and name not in SKIPPED_NAMES
MIN_AGE_SECONDS = 600

_logger = logger.bind(module="backups")


@dataclass(frozen=True)
class ReconcileReport:
    orphan_files: tuple[str, ...] = ()        # relative to upload_path: no media_files row names them
    stale_derived: tuple[str, ...] = ()       # relative to upload_path: posters / storyboards of ids without a row
    returned_originals: tuple[str, ...] = ()  # relative to upload_path, under _orphaned: a row names them again
    missing_originals: tuple[int, ...] = ()   # media_files ids whose original is nowhere
    orphan_packages: tuple[str, ...] = ()     # <user_id>/<certificate_id> folders no user_certificates row names
    failed: tuple[str, ...] = ()              # what apply could not do (logged)


def _stamp(now: datetime) -> str:
    return f"{now.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}"


def _old(path: Path, now: datetime) -> bool:
    """Older than MIN_AGE_SECONDS; a file gone meanwhile or a broken symlink is not (it is left alone)"""
    try:
        return path.stat().st_mtime <= now.timestamp() - MIN_AGE_SECONDS
    except FileNotFoundError:
        return False


def _media_files(root: Path, now: datetime):
    """Relative paths of the old enough files of the media directory, outside _derived, _tmp and _orphaned"""
    for current, dirs, files in os.walk(root):
        here = Path(current)
        dirs[:] = [d for d in dirs if _ours(d) and not (here == root and d in SKIPPED_TOP)]
        for name in files:
            if not _ours(name):
                continue
            path = here / name
            if _old(path, now):
                yield path.relative_to(root).as_posix()


def _derived(root: Path, now: datetime) -> list[tuple[str, int]]:
    """(relative path, media id) of the old enough posters and storyboard folders"""
    found = []
    posters = root / POSTER_DIR
    if posters.is_dir():
        for path in sorted(posters.glob("*/*.jpg")):
            if path.stem.isdigit() and _old(path, now):
                found.append((path.relative_to(root).as_posix(), int(path.stem)))
    storyboards = root / STORYBOARD_DIR
    if storyboards.is_dir():
        for path in sorted(storyboards.glob("*/*")):
            if path.is_dir() and path.name.isdigit() and _old(path, now):
                found.append((path.relative_to(root).as_posix(), int(path.name)))
    return found


def _relative(file_path: str, root: Path) -> Optional[str]:
    """A row's path as the walk sees it: relative to root, forward slashes; None for an absolute path outside root
    (rows from before migration 001 may hold absolute paths, rows written on Windows backslashes)"""
    path = Path(file_path.replace("\\", "/"))
    if path.is_absolute():
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            return None
    return PurePosixPath(file_path.replace("\\", "/")).as_posix()


async def _rows(session: AsyncSession) -> list:
    return list((await session.exec(select(MediaFile.id, MediaFile.file_path, MediaFile.stored_filename))).all())


def _packages(store: Path, named: set[str], now: datetime) -> list[str]:
    orphans = []
    for user_dir in sorted(store.iterdir()):
        if not user_dir.is_dir() or not user_dir.name.isdigit():
            continue
        for folder in sorted(user_dir.iterdir()):
            key = f"{user_dir.name}/{folder.name}"
            if folder.is_dir() and folder.name.isdigit() and key not in named and _old(folder, now):
                orphans.append(key)
    return orphans


async def plan_reconcile(session: AsyncSession, upload_path: Path, store_dir: Optional[Path],
                         now: datetime) -> ReconcileReport:
    # Files first, rows after: a row is committed before its file or poster lands, so every file listed here has
    # its row in the snapshot read below (an upload during the walk is never taken for an orphan)
    files = list(_media_files(upload_path, now))
    derived = _derived(upload_path, now)
    rows = await _rows(session)
    referenced = {rel for rel in (_relative(file_path, upload_path) for _, file_path, _ in rows) if rel}
    stored_names = {stored for _, _, stored in rows}
    ids = {media_id for media_id, _, _ in rows}
    # never move a file a row names by its unique stored name, wherever its path says it is
    orphan_files = sorted(rel for rel in files if rel not in referenced and PurePosixPath(rel).name not in stored_names)
    returned, missing = [], []
    for media_id, file_path, _ in sorted(rows):
        rel = _relative(file_path, upload_path)
        if (upload_path / rel).exists() if rel else Path(file_path).exists():
            continue
        if rel is None:
            missing.append(media_id)
            continue
        copies = sorted((upload_path / ORPHANED_DIR).glob(f"*/{rel}")) if (upload_path / ORPHANED_DIR).is_dir() else []
        if copies:
            returned.append(copies[-1].relative_to(upload_path).as_posix())
        else:
            missing.append(media_id)
    packages = ()
    if store_dir is not None and store_dir.is_dir():
        named = {Path(d).as_posix() for d in (await session.exec(
            select(UserCertificate.files_dir).where(UserCertificate.files_dir.is_not(None)))).all()}
        packages = tuple(_packages(store_dir, named, now))
    return ReconcileReport(orphan_files=tuple(orphan_files),
                           stale_derived=tuple(rel for rel, media_id in derived if media_id not in ids),
                           returned_originals=tuple(returned), missing_originals=tuple(missing),
                           orphan_packages=packages)


def _move(src: Path, dst: Path, failed: list, label: str) -> bool:
    try:
        if dst.exists():
            raise FileExistsError(str(dst))
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.replace(src, dst)
        return True
    except OSError as exc:
        _logger.error(f"Reconcile: {label} not moved ({type(exc).__name__}: {exc})")
        failed.append(label)
        return False


def apply_reconcile(report: ReconcileReport, upload_path: Path, store_dir: Optional[Path],
                    now: datetime) -> ReconcileReport:
    """Do what report found (each path checked again); the report of what was done"""
    stamp, failed = _stamp(now), []
    moved = [rel for rel in report.orphan_files
             if (upload_path / rel).is_file() and _old(upload_path / rel, now)
             and _move(upload_path / rel, upload_path / ORPHANED_DIR / stamp / rel, failed, rel)]
    deleted = []
    for rel in report.stale_derived:
        path = upload_path / rel
        try:
            if not _old(path, now):
                continue
            shutil.rmtree(path) if path.is_dir() else path.unlink()
            deleted.append(rel)
        except FileNotFoundError:
            pass
        except OSError as exc:
            _logger.error(f"Reconcile: {rel} not deleted ({type(exc).__name__}: {exc})")
            failed.append(rel)
    returned = []
    for rel in report.returned_originals:
        original = Path(*Path(rel).parts[2:])          # _orphaned/<stamp>/<the row's path>
        if _move(upload_path / rel, upload_path / original, failed, rel):
            returned.append(rel)
    packages = []
    if store_dir is not None:
        quarantine = store_dir / QUARANTINE_DIR / stamp
        for key in report.orphan_packages:
            if (store_dir / key).is_dir():
                quarantine.mkdir(mode=0o700, parents=True, exist_ok=True)
                if _move(store_dir / key, quarantine / key, failed, key):
                    packages.append(key)
    for rel in moved:
        _logger.info(f"Reconcile: moved {rel} to {ORPHANED_DIR}/{stamp}/")
    for key in packages:
        _logger.info(f"Reconcile: quarantined certificate package {key}")
    return ReconcileReport(orphan_files=tuple(moved), stale_derived=tuple(deleted), returned_originals=tuple(returned),
                           missing_originals=report.missing_originals, orphan_packages=tuple(packages),
                           failed=tuple(failed))
