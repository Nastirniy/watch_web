"""Restore, undo and deep verify (spec section 4).

restore: check the backup, load it into <db>_restore, check its tables and revision against the manifest, carry the
final security state over from the live data (carry_over.py), then one RENAME TABLE moves every live table to
<db>_before_<ts> and every restored table into <db>. The live database is never touched before the swap; any failure
before it drops the staging schema. undo_restore swaps a kept schema back the same way.

The database steps go through a RestoreBackend; MariaDbBackend is the real one (a sync pymysql engine and the
mariadb client), the tests use a recording fake.
"""
import gzip
import hashlib
import os
import subprocess
import tempfile
import threading
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Callable, Optional, Protocol

import sqlalchemy as sa
from loguru import logger
from sqlalchemy.pool import NullPool

from app.backend.config import BackupConfig, DatabaseConfig
from app.backend.models.audit import AuditLog
from app.backend.services.backups.carry_over import (
    CarryOverResult, OrphanCertificates, carry_over, user_state_differences,
)
from app.backend.services.backups.catalog import scan
from app.backend.services.backups.client_tools import ClientTools, defaults_file
from app.backend.services.backups.failures import BackupFailure
from app.backend.services.backups.locking import writer_lock
from app.backend.services.backups.manifest import Manifest, ManifestError, read_manifest
from app.backend.services.backups.schemas import (
    LIST_TABLES, OTHER_CONNECTIONS, SCHEMA_CHARSET, UNSUPPORTED_OBJECTS, KeptSchema, is_droppable, kept_name,
    kept_pattern, kept_schemas_query, parse_kept, quote, rename_statement, staging_name, verify_name,
)

ALEMBIC_INI = Path(__file__).resolve().parents[4] / "alembic.ini"
AUDIT_RESOURCE = "database"
CHUNK = 1024 * 1024
STDERR_LOG_LIMIT = 2000

_logger = logger.bind(module="backups")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RestoreRefused(Exception):
    """Nothing was changed. code: not_found, checksum_mismatch, unknown_revision, unsupported_objects, connections,
    orphan_certificates (serials), nothing_to_undo"""

    def __init__(self, code: str, serials: tuple[str, ...] = ()):
        super().__init__(code)
        self.code = code
        self.serials = serials


@dataclass(frozen=True)
class RestorePlan:
    backup_id: str
    label: str
    started_at: datetime
    backup_revision: Optional[str]
    live_revision: Optional[str]
    other_connections: int


@dataclass(frozen=True)
class RestoreResult:
    kept_schema: str
    carry: CarryOverResult
    user_differences: list


@dataclass(frozen=True)
class VerifyResult:
    backup_id: str
    checksum_ok: bool
    gzip_ok: bool
    tables_ok: Optional[bool]       # None without --deep (or when the file is already bad)
    revision_ok: Optional[bool]


class RestoreBackend(Protocol):
    def unsupported_objects(self, schema: str) -> list[str]: ...
    def other_connections(self, schema: str) -> int: ...
    def create_schema(self, schema: str) -> None: ...
    def drop_schema(self, schema: str) -> None: ...
    def load_dump(self, path: Path, schema: str) -> None: ...
    def list_tables(self, schema: str) -> list[str]: ...
    def revision(self, schema: str) -> Optional[str]: ...
    def carry_over(self, outgoing: str, incoming: str, now: datetime, skip_orphans: bool) -> CarryOverResult: ...
    def user_differences(self, outgoing: str, incoming: str) -> list: ...
    def swap(self, db: str, live: list[str], incoming: list[str], source: str, keep_as: str) -> None: ...
    def audit(self, schema: str, action: str, details: dict) -> None: ...
    def kept_schemas(self, db: str) -> list[KeptSchema]: ...


@lru_cache(maxsize=1)
def _script():
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    return ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))


def known_revisions() -> set[str]:
    return {revision.revision for revision in _script().walk_revisions()}


def code_head() -> str:
    return _script().get_current_head()


def sync_engine(db: DatabaseConfig) -> sa.Engine:
    url = sa.engine.URL.create("mysql+pymysql", username=db.user, password=db.password, host=db.host, port=db.port,
                               database=db.name, query={"charset": "utf8mb4"})
    return sa.create_engine(url, poolclass=NullPool)


class MariaDbBackend:
    """The database steps on MariaDB (MySQL 8 too): a sync engine as the app user and the client binary"""

    def __init__(self, db: DatabaseConfig, tools: ClientTools, timeout_seconds: int, engine: sa.Engine = None):
        self.db = db
        self.tools = tools
        self.timeout_seconds = timeout_seconds
        self.engine = engine or sync_engine(db)

    def unsupported_objects(self, schema: str) -> list[str]:
        with self.engine.connect() as conn:
            return [row[0] for row in conn.execute(UNSUPPORTED_OBJECTS, {"schema": schema})]

    def other_connections(self, schema: str) -> int:
        with self.engine.connect() as conn:
            return int(conn.execute(OTHER_CONNECTIONS, {"schema": schema}).scalar() or 0)

    def create_schema(self, schema: str) -> None:
        with self.engine.begin() as conn:
            charset, collation = conn.execute(SCHEMA_CHARSET, {"schema": self.db.name}).one()
            conn.exec_driver_sql(f"CREATE DATABASE {quote(schema)} CHARACTER SET {charset} COLLATE {collation}")

    def drop_schema(self, schema: str) -> None:
        if not is_droppable(self.db.name, schema):
            raise ValueError(f"refusing to drop schema {schema}")
        with self.engine.begin() as conn:
            conn.exec_driver_sql(f"DROP DATABASE IF EXISTS {quote(schema)}")

    def load_dump(self, path: Path, schema: str) -> None:
        with defaults_file(self.db) as credentials, tempfile.TemporaryFile() as stderr:
            command = [self.tools.client, f"--defaults-extra-file={credentials}", "--default-character-set=utf8mb4",
                       schema]
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=stderr)
            timed_out = threading.Event()

            def kill():
                timed_out.set()
                process.kill()

            timer = threading.Timer(self.timeout_seconds, kill)
            timer.start()
            try:
                with gzip.open(path, "rb") as dump:
                    for block in iter(lambda: dump.read(CHUNK), b""):
                        process.stdin.write(block)
                process.stdin.close()
                exit_code = process.wait()
            except (BrokenPipeError, OSError, EOFError, zlib.error) as exc:
                _logger.error(f"Backups: load into {schema} broke off ({type(exc).__name__}: {exc})")
                exit_code = -1
            finally:
                timer.cancel()
                if process.poll() is None:
                    process.kill()
                    process.wait()
            if timed_out.is_set():
                raise BackupFailure("timeout")
            if exit_code != 0:
                stderr.seek(0)
                _logger.error(f"Backups: {Path(self.tools.client).name} exited with {exit_code}: "
                              f"{stderr.read(STDERR_LOG_LIMIT).decode('utf-8', errors='replace')}")
                raise BackupFailure("load_failed")

    def list_tables(self, schema: str) -> list[str]:
        with self.engine.connect() as conn:
            return [row[0] for row in conn.execute(LIST_TABLES, {"schema": schema})]

    def revision(self, schema: str) -> Optional[str]:
        if "alembic_version" not in self.list_tables(schema):
            return None
        with self.engine.connect() as conn:
            return conn.exec_driver_sql(f"SELECT version_num FROM {quote(schema)}.alembic_version").scalar()

    def carry_over(self, outgoing: str, incoming: str, now: datetime, skip_orphans: bool) -> CarryOverResult:
        with self.engine.begin() as conn:
            return carry_over(conn, outgoing, incoming, now, skip_orphans=skip_orphans)

    def user_differences(self, outgoing: str, incoming: str) -> list:
        with self.engine.connect() as conn:
            return user_state_differences(conn, outgoing, incoming)

    def swap(self, db: str, live: list[str], incoming: list[str], source: str, keep_as: str) -> None:
        with self.engine.begin() as conn:
            conn.exec_driver_sql(rename_statement(db, live, incoming, source, keep_as))

    def audit(self, schema: str, action: str, details: dict) -> None:
        table = AuditLog.__table__.to_metadata(sa.MetaData(), schema=schema)
        with self.engine.begin() as conn:
            conn.execute(sa.insert(table).values(action=action, resource_type=AUDIT_RESOURCE, success=True,
                                                 details=details, created_at=utcnow().replace(tzinfo=None)))

    def kept_schemas(self, db: str) -> list[KeptSchema]:
        with self.engine.connect() as conn:
            rows = conn.execute(kept_schemas_query(), {"pattern": kept_pattern(db)}).all()
        return parse_kept(db, rows)


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def _gzip_ok(path: Path) -> bool:
    try:
        with gzip.open(path, "rb") as f:
            while f.read(CHUNK):
                pass
        return True
    except (OSError, EOFError, zlib.error):
        return False


def _find(cfg: BackupConfig, ref: str) -> tuple[Path, Manifest]:
    """The folder and manifest of a backup id, or of the newest backup for "latest" """
    directory = Path(cfg.dir)
    if ref == "latest":
        manifests, _ = scan(directory)
        if not manifests:
            raise RestoreRefused("not_found")
        manifest = max(manifests, key=lambda m: m.started_at)
        return directory / manifest.id, manifest
    folder = directory / ref
    if "/" in ref or "\\" in ref or ref.startswith(".") or not folder.is_dir():
        raise RestoreRefused("not_found")
    try:
        return folder, read_manifest(folder)
    except ManifestError as exc:
        _logger.error(f"Backups: {ref}: {exc.reason}")
        raise RestoreRefused("not_found")


def _checked(cfg: BackupConfig, ref: str) -> tuple[Path, Manifest]:
    folder, manifest = _find(cfg, ref)
    if _sha256_of(folder / manifest.file) != manifest.sha256:
        raise RestoreRefused("checksum_mismatch")
    if manifest.migration_revision not in known_revisions():
        raise RestoreRefused("unknown_revision")
    return folder, manifest


def _guard(backend: RestoreBackend, db: str, force_connections: bool) -> None:
    unsupported = backend.unsupported_objects(db)
    if unsupported:
        _logger.error(f"Backups: unsupported objects in {db}: {', '.join(unsupported)}")
        raise RestoreRefused("unsupported_objects")
    if not force_connections and backend.other_connections(db) > 0:
        raise RestoreRefused("connections")


def plan_restore(cfg: BackupConfig, db: DatabaseConfig, ref: str, *, backend: RestoreBackend) -> RestorePlan:
    _, manifest = _checked(cfg, ref)
    return RestorePlan(backup_id=manifest.id, label=manifest.label.value, started_at=manifest.started_at,
                       backup_revision=manifest.migration_revision, live_revision=backend.revision(db.name),
                       other_connections=backend.other_connections(db.name))


def _carry(backend: RestoreBackend, db: str, incoming: str, now: datetime, skip_orphans: bool) -> CarryOverResult:
    try:
        return backend.carry_over(db, incoming, now, skip_orphans)
    except OrphanCertificates as exc:
        raise RestoreRefused("orphan_certificates", serials=exc.serials)


def _details(carry: CarryOverResult, keep_as: str) -> dict:
    return {"kept_schema": keep_as, "revocations": carry.revocations, "certificates_added": carry.certificates_added,
            "orphans_skipped": len(carry.orphan_serials), "crl_number": carry.crl_number,
            "sessions_ended": carry.sessions_ended}


def restore_backup(cfg: BackupConfig, db: DatabaseConfig, ref: str, *, backend: RestoreBackend,
                   force_connections: bool = False, skip_orphans: bool = False,
                   now: Callable[[], datetime] = utcnow) -> RestoreResult:
    with writer_lock(Path(cfg.dir)):
        folder, manifest = _checked(cfg, ref)
        _guard(backend, db.name, force_connections)
        staging = staging_name(db.name)
        backend.drop_schema(staging)
        backend.create_schema(staging)
        keep_as, swapped = None, False
        try:
            backend.load_dump(folder / manifest.file, staging)
            tables = backend.list_tables(staging)
            if set(tables) != set(manifest.tables) or backend.revision(staging) != manifest.migration_revision:
                _logger.error(f"Backups: {manifest.id} loaded into {staging} does not match its manifest")
                raise BackupFailure("load_failed")
            carry = _carry(backend, db.name, staging, now(), skip_orphans)
            differences = backend.user_differences(db.name, staging)
            keep_as = kept_name(db.name, now())
            backend.create_schema(keep_as)
            backend.swap(db.name, backend.list_tables(db.name), tables, staging, keep_as)
            swapped = True
        finally:
            if not swapped:
                backend.drop_schema(staging)
                if keep_as is not None:
                    backend.drop_schema(keep_as)
        backend.drop_schema(staging)
        backend.audit(db.name, "restore", {"backup_id": manifest.id, "label": manifest.label.value,
                                           "backup_revision": manifest.migration_revision, **_details(carry, keep_as)})
        _logger.info(f"Backups: restored {manifest.id}; the replaced tables are kept in {keep_as}")
        return RestoreResult(kept_schema=keep_as, carry=carry, user_differences=differences)


def undo_restore(cfg: BackupConfig, db: DatabaseConfig, schema: Optional[str], *, backend: RestoreBackend,
                 force_connections: bool = False, skip_orphans: bool = False,
                 now: Callable[[], datetime] = utcnow) -> RestoreResult:
    """Swap a kept schema (the newest, or the named one) back into db; the tables it replaces are kept in turn"""
    with writer_lock(Path(cfg.dir)):
        kept = backend.kept_schemas(db.name)
        if schema is not None:
            kept = [k for k in kept if k.name == schema]
        if not kept:
            raise RestoreRefused("nothing_to_undo")
        source = max(kept, key=lambda k: k.created_at).name
        if backend.revision(source) not in known_revisions():
            raise RestoreRefused("unknown_revision")
        _guard(backend, db.name, force_connections)
        carry = _carry(backend, db.name, source, now(), skip_orphans)
        differences = backend.user_differences(db.name, source)
        keep_as = kept_name(db.name, now())
        backend.create_schema(keep_as)
        try:
            backend.swap(db.name, backend.list_tables(db.name), backend.list_tables(source), source, keep_as)
        except BaseException:
            backend.drop_schema(keep_as)
            raise
        backend.drop_schema(source)
        backend.audit(db.name, "undo_restore", {"source_schema": source, **_details(carry, keep_as)})
        _logger.info(f"Backups: swapped {source} back in; the replaced tables are kept in {keep_as}")
        return RestoreResult(kept_schema=keep_as, carry=carry, user_differences=differences)


def prune_kept_schemas(cfg: BackupConfig, db: DatabaseConfig, *, backend: RestoreBackend, now: datetime,
                       dry_run: bool = False) -> list[str]:
    """Drop the kept <db>_before_* schemas older than keep_previous_days; their names, oldest first"""
    oldest = now - timedelta(days=cfg.keep_previous_days)
    doomed = [k.name for k in backend.kept_schemas(db.name) if k.created_at < oldest]
    if not dry_run:
        for schema in doomed:
            backend.drop_schema(schema)
            _logger.info(f"Backups: dropped kept schema {schema} (older than {cfg.keep_previous_days} days)")
    return doomed


def verify_backup(cfg: BackupConfig, db: DatabaseConfig, ref: str, *, deep: bool,
                  backend: Optional[RestoreBackend] = None) -> VerifyResult:
    folder, manifest = _find(cfg, ref)
    path = folder / manifest.file
    checksum_ok = _sha256_of(path) == manifest.sha256
    gzip_ok = _gzip_ok(path)
    if not deep or not (checksum_ok and gzip_ok):
        return VerifyResult(backup_id=manifest.id, checksum_ok=checksum_ok, gzip_ok=gzip_ok, tables_ok=None,
                            revision_ok=None)
    scratch = verify_name(db.name)
    with writer_lock(Path(cfg.dir)):
        backend.drop_schema(scratch)
        try:
            backend.create_schema(scratch)
            backend.load_dump(path, scratch)
            tables_ok = set(backend.list_tables(scratch)) == set(manifest.tables)
            revision_ok = backend.revision(scratch) == manifest.migration_revision
        finally:
            backend.drop_schema(scratch)
    return VerifyResult(backup_id=manifest.id, checksum_ok=True, gzip_ok=True, tables_ok=tables_ok,
                        revision_ok=revision_ok)
