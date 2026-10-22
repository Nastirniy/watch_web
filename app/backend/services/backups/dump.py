"""create: one backup with mariadb-dump, its off-host copy and retention (spec section 3).

The dump streams through gzip into <dir>/.tmp-<id>/; the manifest is written last and the folder renamed to <id>,
so a folder named like a backup is always complete. The tables and the migration revision of the manifest come from
the dump stream itself (the same snapshot as the data).
"""
import gzip
import hashlib
import io
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Callable, Optional

from loguru import logger

from app.backend.config import BackupConfig, DatabaseConfig
from app.backend.enums.general import BackupLabel
from app.backend.services.backups.catalog import TMP_PREFIX, LastRun, scan, write_last_run
from app.backend.services.backups.client_tools import ClientTools, defaults_file, find_tools
from app.backend.services.backups.failures import BackupFailure
from app.backend.services.backups.locking import writer_lock
from app.backend.services.backups.manifest import (
    DIR_MODE, DUMP_FILE, FORMAT_VERSION, MANIFEST_FILE, PRIVATE_MODE, SHARED_MODE, Manifest, backup_id,
    read_manifest, write_manifest,
)
from app.backend.services.backups.retention import RetentionRules, select_to_delete

DUMP_ARGS = ("--single-transaction", "--quick", "--hex-blob", "--default-character-set=utf8mb4",
             "--no-tablespaces", "--skip-lock-tables")
CHUNK = 1024 * 1024
CARRY = 64 * 1024          # text kept across chunk borders for the line patterns below
STDERR_LOG_LIMIT = 2000

_CREATE_TABLE = re.compile(r"^CREATE TABLE `((?:[^`]|``)+)`", re.MULTILINE)
_REVISION = re.compile(r"^INSERT INTO `alembic_version` VALUES \('([^']+)'\);", re.MULTILINE)
_SERVER_VERSION = re.compile(r"^-- Server version\t(\S+)", re.MULTILINE)

_logger = logger.bind(module="backups")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class CreateResult:
    manifest: Manifest
    copied: Optional[bool]          # None without copy_dir or with --no-copy
    pruned: tuple[str, ...]


class _HashingWriter(io.RawIOBase):
    """A file that hashes and counts what is written to it"""

    def __init__(self, target: BinaryIO):
        self.target = target
        self.sha256 = hashlib.sha256()
        self.size = 0

    def writable(self) -> bool:
        return True

    def write(self, data) -> int:
        self.target.write(data)
        self.sha256.update(data)
        self.size += len(data)
        return len(data)


@dataclass
class _DumpFacts:
    tables: list
    revision: Optional[str] = None
    server_version: str = ""
    sha256: str = ""
    size: int = 0


def _scan_text(text: str, facts: _DumpFacts) -> None:
    for match in _CREATE_TABLE.finditer(text):
        name = match.group(1).replace("``", "`")
        if name not in facts.tables:
            facts.tables.append(name)
    for match in _REVISION.finditer(text):
        facts.revision = match.group(1)
    if not facts.server_version:
        match = _SERVER_VERSION.search(text)
        if match:
            facts.server_version = match.group(1)


def _dump(tools: ClientTools, db: DatabaseConfig, target: Path, timeout_seconds: int) -> _DumpFacts:
    facts = _DumpFacts(tables=[])
    with defaults_file(db) as credentials, tempfile.TemporaryFile() as stderr:
        command = [tools.dump, f"--defaults-extra-file={credentials}", *DUMP_ARGS, db.name]
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=stderr, stdin=subprocess.DEVNULL)
        timed_out = threading.Event()

        def kill():
            timed_out.set()
            process.kill()

        timer = threading.Timer(timeout_seconds, kill)
        timer.start()
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, PRIVATE_MODE)
            with os.fdopen(fd, "wb") as raw:
                hashing = _HashingWriter(raw)
                with gzip.GzipFile(fileobj=hashing, mode="wb", compresslevel=6, mtime=0) as compressed:
                    carry = ""
                    while True:
                        chunk = process.stdout.read(CHUNK)
                        if not chunk:
                            break
                        compressed.write(chunk)
                        text = carry + chunk.decode("utf-8", errors="replace")
                        _scan_text(text, facts)
                        carry = text[-CARRY:]
                facts.sha256 = hashing.sha256.hexdigest()
                facts.size = hashing.size
            exit_code = process.wait()
        finally:
            timer.cancel()
            if process.poll() is None:
                process.kill()
                process.wait()
            process.stdout.close()
        if timed_out.is_set():
            raise BackupFailure("timeout")
        if exit_code != 0:
            stderr.seek(0)
            _logger.error(f"Backups: {Path(tools.dump).name} exited with {exit_code}: "
                          f"{stderr.read(STDERR_LOG_LIMIT).decode('utf-8', errors='replace')}")
            raise BackupFailure("dump_failed")
    return facts


def _tool_version(tools: ClientTools) -> str:
    try:
        result = subprocess.run([tools.dump, "--version"], capture_output=True, timeout=30, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return Path(tools.dump).name
    lines = result.stdout.decode("utf-8", errors="replace").strip().splitlines()
    return lines[0].strip() if lines else Path(tools.dump).name


def _remove_stale_tmp(directory: Path, timeout_seconds: int) -> None:
    oldest = time.time() - timeout_seconds
    for child in directory.iterdir():
        if child.name.startswith(TMP_PREFIX) and child.is_dir() and child.stat().st_mtime < oldest:
            shutil.rmtree(child, ignore_errors=True)


def _check_space(directory: Path, cfg: BackupConfig) -> None:
    manifests, _ = scan(directory)
    last = max(manifests, key=lambda m: m.started_at).size_bytes if manifests else 0
    need = int(last * 1.5) + cfg.min_free_mb * 2 ** 20
    free = shutil.disk_usage(directory).free
    if free < need:
        _logger.error(f"Backups: {free} bytes free in the backup directory, {need} needed")
        raise BackupFailure("no_space")


def _new_dir(path: Path) -> None:
    path.mkdir(mode=DIR_MODE)
    os.chmod(path, DIR_MODE)


def _copy_file(src: Path, dst: Path, mode: int = PRIVATE_MODE) -> None:
    shutil.copyfile(src, dst)
    os.chmod(dst, mode)


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def _copy(folder: Path, copy_dir: Path, manifest: Manifest) -> None:
    """Copy a complete backup folder to copy_dir through .tmp-<id>, checking the dump's checksum from the copy"""
    tmp = copy_dir / f"{TMP_PREFIX}{manifest.id}"
    try:
        if tmp.exists():
            shutil.rmtree(tmp)
        _new_dir(tmp)
        _copy_file(folder / manifest.file, tmp / manifest.file)
        _copy_file(folder / MANIFEST_FILE, tmp / MANIFEST_FILE, SHARED_MODE)
        if _sha256_of(tmp / manifest.file) != manifest.sha256:
            _logger.error(f"Backups: the copy of {manifest.id} in copy_dir does not match its checksum")
            raise BackupFailure("copy_failed")
        os.replace(tmp, copy_dir / manifest.id)
    except OSError as exc:
        _logger.error(f"Backups: copy of {manifest.id} to {copy_dir} failed ({type(exc).__name__}: {exc})")
        shutil.rmtree(tmp, ignore_errors=True)
        raise BackupFailure("copy_failed")
    except BackupFailure:
        shutil.rmtree(tmp, ignore_errors=True)
        raise


def _prune_directory(directory: Path, rules: RetentionRules, now: datetime, dry_run: bool) -> list[str]:
    manifests, _ = scan(directory)
    doomed = select_to_delete(manifests, rules, now)
    if not dry_run:
        for backup in doomed:
            shutil.rmtree(directory / backup)
            _logger.info(f"Backups: deleted {backup} from {directory} (retention)")
    return doomed


def prune(cfg: BackupConfig, now: datetime, dry_run: bool = False) -> list[str]:
    """Retention on dir and copy_dir (each on its own); the ids deleted (or that would be), sorted"""
    doomed = set(_prune_directory(Path(cfg.dir), RetentionRules.from_config(cfg), now, dry_run))
    if cfg.copy_dir:
        try:
            doomed |= set(_prune_directory(Path(cfg.copy_dir), RetentionRules.from_config(cfg), now, dry_run))
        except OSError as exc:
            _logger.warning(f"Backups: copy_dir not pruned ({type(exc).__name__}: {exc})")
    return sorted(doomed)


def create_backup(cfg: BackupConfig, db: DatabaseConfig, label: BackupLabel, *, copy: bool = True,
                  now: Callable[[], datetime] = utcnow, tools: Optional[ClientTools] = None,
                  object_guard: Optional[Callable[[], list]] = None) -> CreateResult:
    directory = Path(cfg.dir)
    if not directory.exists():
        directory.mkdir(parents=True)
        os.chmod(directory, DIR_MODE)
    with writer_lock(directory):
        started = now()
        try:
            result = _create(cfg, db, label, started, copy, now, tools, object_guard)
        except BackupFailure as exc:
            write_last_run(directory, LastRun(outcome="failed", started_at=started, finished_at=now(),
                                              error_code=exc.code, backup_id=None))
            raise
        except BaseException:
            write_last_run(directory, LastRun(outcome="failed", started_at=started, finished_at=now(),
                                              error_code="error", backup_id=None))
            raise
    return result


def _create(cfg, db, label, started, copy, now, tools, object_guard) -> CreateResult:
    directory = Path(cfg.dir)
    _remove_stale_tmp(directory, cfg.dump_timeout_seconds)
    if object_guard is not None:
        unsupported = object_guard()
        if unsupported:
            _logger.error(f"Backups: unsupported objects in the database: {', '.join(unsupported)}")
            raise BackupFailure("unsupported_objects")
    _check_space(directory, cfg)
    tools = tools or find_tools()
    backup = backup_id(started, label)
    tmp = directory / f"{TMP_PREFIX}{backup}"
    _new_dir(tmp)
    try:
        facts = _dump(tools, db, tmp / DUMP_FILE, cfg.dump_timeout_seconds)
        version = facts.server_version
        manifest = Manifest(
            format_version=FORMAT_VERSION, id=backup, label=BackupLabel(label), started_at=started,
            finished_at=now(), database=db.name, server_product="mariadb" if "mariadb" in version.lower() else
            ("mysql" if version else ""), server_version=version, dump_tool=_tool_version(tools),
            migration_revision=facts.revision, tables=tuple(facts.tables), file=DUMP_FILE, size_bytes=facts.size,
            sha256=facts.sha256,
        )
        write_manifest(tmp, manifest)
        os.replace(tmp, directory / backup)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    _logger.info(f"Backups: created {backup} ({manifest.size_bytes} bytes, revision {manifest.migration_revision})")

    copied, copy_failure = None, None
    if copy and cfg.copy_dir:
        try:
            _copy(directory / backup, Path(cfg.copy_dir), read_manifest(directory / backup))
            copied = True
        except BackupFailure as exc:
            copied, copy_failure = False, exc
    pruned = tuple(prune(cfg, now()))
    if copy_failure is not None:
        raise copy_failure
    write_last_run(directory, LastRun(outcome="ok", started_at=started, finished_at=now(), error_code=None,
                                      backup_id=backup))
    return CreateResult(manifest=manifest, copied=copied, pruned=pruned)
