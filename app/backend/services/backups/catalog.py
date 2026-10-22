"""The catalog: backup folders on disk and their status. Read-only (the API, /health and the CLI read it).

A folder is a backup when its manifest is valid; .tmp-* folders are runs in progress and .state holds the outcome of
the last run (last-run.json) and the lock. A folder with a damaged manifest is reported as incomplete, never as an
error of the whole catalog.
"""
import asyncio
import json
import os
import shutil
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger

from app.backend.config import BackupConfig
from app.backend.enums.errors import ErrorCode
from app.backend.errors.coded import CodedError
from app.backend.services.backups.manifest import (
    DIR_MODE, SHARED_MODE, Manifest, ManifestError, read_manifest, require_dump,
)

STATE_DIR = ".state"
LAST_RUN_FILE = "last-run.json"
TMP_PREFIX = ".tmp-"

_logger = logger.bind(module="backups")

# The API reads the catalog on a thread of its own with a timeout: a hard network mount that stops answering blocks
# that thread, never the default executor (uploads, certificate issuing); one read is in flight at a time
CATALOG_TIMEOUT_SECONDS = 5.0
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="backup-catalog")
_in_flight: Optional[tuple[tuple, Future]] = None   # ((dir, copy_dir), the read)


@dataclass(frozen=True)
class LastRun:
    """The outcome of the last create: ok or failed, with an error code (never a path or tool output)"""
    outcome: str
    started_at: datetime
    finished_at: datetime
    error_code: Optional[str]
    backup_id: Optional[str]


@dataclass(frozen=True)
class BackupEntry:
    manifest: Manifest
    copied: Optional[bool]          # None when there is no copy_dir or it cannot be read


@dataclass(frozen=True)
class Incomplete:
    name: str
    reason: str


@dataclass(frozen=True)
class CopyState:
    configured: bool
    reachable: bool
    missing: int                    # backups without a valid copy


@dataclass(frozen=True)
class BackupSummary:
    last_success_at: Optional[datetime]
    last_success_id: Optional[str]
    age_seconds: Optional[int]
    stale: bool
    stale_after_hours: int
    last_run: Optional[LastRun]
    count: int
    total_size_bytes: int
    free_bytes: int
    copy: CopyState


@dataclass(frozen=True)
class Catalog:
    entries: tuple[BackupEntry, ...]   # newest first
    incomplete: tuple[Incomplete, ...]
    summary: BackupSummary


def write_last_run(directory: Path, run: LastRun) -> None:
    state = directory / STATE_DIR
    state.mkdir(mode=DIR_MODE, exist_ok=True)
    data = {
        "outcome": run.outcome, "started_at": run.started_at.astimezone(timezone.utc).isoformat(),
        "finished_at": run.finished_at.astimezone(timezone.utc).isoformat(), "error_code": run.error_code,
        "backup_id": run.backup_id,
    }
    tmp = state / (LAST_RUN_FILE + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, SHARED_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f)
    os.chmod(tmp, SHARED_MODE)
    os.replace(tmp, state / LAST_RUN_FILE)


def read_last_run(directory: Path) -> Optional[LastRun]:
    path = directory / STATE_DIR / LAST_RUN_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        outcome, error_code, backup = data["outcome"], data["error_code"], data["backup_id"]
        if outcome not in ("ok", "failed") or not all(v is None or isinstance(v, str) for v in (error_code, backup)):
            raise ValueError("bad value")
        return LastRun(
            outcome=outcome, started_at=datetime.fromisoformat(data["started_at"]),
            finished_at=datetime.fromisoformat(data["finished_at"]), error_code=error_code, backup_id=backup,
        )
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        _logger.warning(f"Backups: {LAST_RUN_FILE} not readable ({type(exc).__name__})")
        return None


def scan(folder: Path) -> tuple[list[Manifest], list[Incomplete]]:
    """Valid manifests and incomplete folders of one directory (OSError when it cannot be listed)"""
    manifests, incomplete = [], []
    for child in sorted(folder.iterdir()):
        if child.name.startswith(".") or not child.is_dir():
            continue
        try:
            manifests.append(require_dump(child, read_manifest(child)))
        except ManifestError as exc:
            incomplete.append(Incomplete(name=child.name, reason=exc.reason))
    return manifests, incomplete


def _copies(cfg: BackupConfig) -> Optional[dict]:
    """sha256 by id of the valid copies; None when copy_dir is not set or cannot be read"""
    if not cfg.copy_dir:
        return None
    try:
        manifests, _ = scan(Path(cfg.copy_dir))
    except OSError as exc:
        _logger.warning(f"Backups: copy_dir {cfg.copy_dir} not readable ({type(exc).__name__})")
        return None
    return {m.id: m.sha256 for m in manifests}


def load_catalog(cfg: BackupConfig, now: datetime) -> Catalog:
    if not cfg.is_configured():
        raise CodedError(ErrorCode.BACKUPS_NOT_CONFIGURED)
    directory = Path(cfg.dir)
    try:
        manifests, incomplete = scan(directory)
        free_bytes = shutil.disk_usage(directory).free
    except OSError as exc:
        _logger.error(f"Backups: dir {cfg.dir} not readable ({type(exc).__name__}: {exc})")
        raise CodedError(ErrorCode.BACKUP_DIRECTORY_UNAVAILABLE)
    manifests.sort(key=lambda m: m.started_at, reverse=True)
    copies = _copies(cfg)
    entries = tuple(BackupEntry(manifest=m, copied=None if copies is None else copies.get(m.id) == m.sha256)
                    for m in manifests)
    newest = manifests[0] if manifests else None
    age = None if newest is None else int((now - newest.finished_at).total_seconds())
    summary = BackupSummary(
        last_success_at=None if newest is None else newest.finished_at,
        last_success_id=None if newest is None else newest.id,
        age_seconds=age,
        stale=age is None or age > cfg.stale_after_hours * 3600,
        stale_after_hours=cfg.stale_after_hours,
        last_run=read_last_run(directory),
        count=len(manifests),
        total_size_bytes=sum(m.size_bytes for m in manifests),
        free_bytes=free_bytes,
        copy=CopyState(configured=bool(cfg.copy_dir), reachable=copies is not None,
                       missing=0 if copies is None else sum(1 for e in entries if e.copied is False)),
    )
    return Catalog(entries=entries, incomplete=tuple(incomplete), summary=summary)


async def load_catalog_async(cfg: BackupConfig, now: datetime) -> Catalog:
    """load_catalog for the API and /health; a read that takes longer than CATALOG_TIMEOUT_SECONDS (a hung share)
    is backup_directory_unavailable, and the next call waits for the same read instead of starting another"""
    global _in_flight
    key = (cfg.dir, cfg.copy_dir)
    if _in_flight is None or _in_flight[1].done() or _in_flight[0] != key:
        _in_flight = (key, _executor.submit(lambda: load_catalog(cfg, now)))
    try:
        return await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(_in_flight[1])), CATALOG_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        _logger.error(f"Backups: reading {cfg.dir} / {cfg.copy_dir or '-'} took longer than "
                      f"{CATALOG_TIMEOUT_SECONDS} s (a share that does not answer?)")
        raise CodedError(ErrorCode.BACKUP_DIRECTORY_UNAVAILABLE)
