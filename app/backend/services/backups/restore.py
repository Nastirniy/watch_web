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
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import Callable, Optional, Protocol

import sqlalchemy as sa
from loguru import logger
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool

from app.backend.config import BackupConfig, DatabaseConfig
from app.backend.enums.audit import AuditEvent
from app.backend.services.backups.carry_over import (
    CarryOverResult, CarryOverUnsupported, _only, _reflect, carry_over, user_state_differences,
)
from app.backend.services.backups.catalog import scan
from app.backend.services.backups.client_tools import ClientTools, child_env, defaults_file
from app.backend.services.backups.failures import BackupFailure
from app.backend.services.backups.locking import writer_lock
from app.backend.services.backups.manifest import Manifest, ManifestError, read_manifest, require_dump
from app.backend.services.backups.schemas import (
    AUTO_INCREMENTS, LIST_TABLES, OTHER_CONNECTIONS, SCHEMA_CHARSET, SCHEMA_SIZE, UNSUPPORTED_OBJECTS, KeptSchema,
    is_droppable,
    kept_name, kept_pattern, kept_schemas_query, parse_kept, quote, raise_statements, rename_statement,
    staging_name, verify_name,
)

ALEMBIC_INI = Path(__file__).resolve().parents[4] / "alembic.ini"
# A line of a dump that would leave the schema it is loaded into, or run a client command (shell, files, another
# connection). mariadb-dump never writes one for a single database (values are escaped onto one line), so such a
# line means a dump made with --databases or by hand: the load stops (unsafe_dump)
_UNSAFE = re.compile(
    r"^[ \t]*(?:USE\b|CONNECT\b|SYSTEM\b|SOURCE\b|\\|(?:CREATE|DROP|ALTER)[ \t]+(?:DATABASE|SCHEMA)\b)",
    re.MULTILINE | re.IGNORECASE)
AUDIT_RESOURCE = "database"
RESTORE_EVENTS = {"restore": AuditEvent.DATABASE_RESTORE, "undo_restore": AuditEvent.DATABASE_UNDO_RESTORE}
CHUNK = 1024 * 1024
STDERR_LOG_LIMIT = 2000

_logger = logger.bind(module="backups")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class RestoreRefused(Exception):
    """Nothing was changed. code: not_found, checksum_mismatch, unknown_revision, other_database, unsupported_objects,
    connections, no_space / space_unknown (the datadir), no_grant (no right on the staging, verify or kept
    schemas), nothing_to_undo, backup_too_old (no table to carry revocations into)"""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


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
    ids_raised: int = 0          # tables whose AUTO_INCREMENT was raised past the replaced data


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
    def schema_size(self, schema: str) -> int: ...
    def create_schema(self, schema: str) -> None: ...
    def drop_schema(self, schema: str) -> None: ...
    def load_dump(self, path: Path, schema: str) -> None: ...
    def list_tables(self, schema: str) -> list[str]: ...
    def revision(self, schema: str) -> Optional[str]: ...
    def carry_over(self, outgoing: str, incoming: str, now: datetime) -> CarryOverResult: ...
    def user_differences(self, outgoing: str, incoming: str) -> list: ...
    def bump_auto_increment(self, outgoing: str, incoming: str) -> int: ...
    def swap(self, db: str, live: list[str], incoming: list[str], source: str, keep_as: str) -> None: ...
    def audit(self, schema: str, action: str, details: dict) -> None: ...
    def kept_schemas(self, db: str) -> list[KeptSchema]: ...


@lru_cache(maxsize=1)
def _script():
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    return ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))


def unsafe_statement(text: str) -> Optional[str]:
    """The first line of text that would leave the staging schema or run a client command, None when there is none"""
    match = _UNSAFE.search(text)
    if match is None:
        return None
    end = text.find("\n", match.start())
    return text[match.start():end if end >= 0 else len(text)][:80]


class _CheckedLines:
    """The dump in whole lines, each checked before it is handed on: a block may end (and the next one start) inside
    a line, where a value may begin with a backslash; the unfinished last line waits for the next block"""

    def __init__(self):
        self.pending = b""
        self.unsafe: Optional[str] = None

    def take(self, block: bytes) -> bytes:
        """The checked whole lines up to the end of block (b"" once an unsafe line was found)"""
        data = self.pending + block
        cut = data.rfind(b"\n") + 1
        self.pending, lines = data[cut:], data[:cut]
        return self._checked(lines)

    def rest(self) -> bytes:
        """The checked last line, at the end of the dump"""
        lines, self.pending = self.pending, b""
        return self._checked(lines)

    def _checked(self, lines: bytes) -> bytes:
        if self.unsafe is None and lines:
            self.unsafe = unsafe_statement(lines.decode("utf-8", errors="replace"))
        return b"" if self.unsafe is not None else lines


def known_revisions() -> set[str]:
    return {revision.revision for revision in _script().walk_revisions()}


def code_head() -> str:
    return _script().get_current_head()


def sync_engine(db: DatabaseConfig) -> sa.Engine:
    url = sa.engine.URL.create("mysql+pymysql", username=db.user, password=db.password, host=db.host, port=db.port,
                               database=db.name, query={"charset": "utf8mb4"})
    return sa.create_engine(url, poolclass=NullPool)


# Access denied to a database / to a table: the account lacks the grant on the staging and kept schemas
_ACCESS_DENIED = frozenset({1044, 1142})


class _granted:
    """Turn an access-denied error of a schema statement into RestoreRefused("no_grant") (BACKUPS.md, grants)"""

    def __init__(self, schema: str):
        self.schema = schema

    def __enter__(self):
        return self

    def __exit__(self, kind, exc, tb):
        if isinstance(exc, OperationalError) and exc.orig is not None and exc.orig.args \
                and exc.orig.args[0] in _ACCESS_DENIED:
            _logger.error(f"Backups: no grant on {self.schema} (GRANT ALL ON `<db>\\_%`.*, BACKUPS.md)")
            raise RestoreRefused("no_grant") from None
        return False


class MariaDbBackend:
    """The database steps on MariaDB (MySQL 8 too): a sync engine as the app user and the client binary"""

    def __init__(self, db: DatabaseConfig, tools: ClientTools, timeout_seconds: int, engine: sa.Engine = None,
                 lock_wait_seconds: int = 60):
        self.db = db
        self.tools = tools
        self.timeout_seconds = timeout_seconds
        self.lock_wait_seconds = int(lock_wait_seconds)
        self.engine = engine or sync_engine(db)

    def unsupported_objects(self, schema: str) -> list[str]:
        with self.engine.connect() as conn:
            return [row[0] for row in conn.execute(UNSUPPORTED_OBJECTS, {"schema": schema})]

    def other_connections(self, schema: str) -> int:
        with self.engine.connect() as conn:
            return int(conn.execute(OTHER_CONNECTIONS, {"schema": schema}).scalar() or 0)

    def schema_size(self, schema: str) -> int:
        with self.engine.connect() as conn:
            return int(conn.execute(SCHEMA_SIZE, {"schema": schema}).scalar() or 0)

    def create_schema(self, schema: str) -> None:
        with _granted(schema), self.engine.begin() as conn:
            charset, collation = conn.execute(SCHEMA_CHARSET, {"schema": self.db.name}).one()
            conn.exec_driver_sql(f"CREATE DATABASE {quote(schema)} CHARACTER SET {charset} COLLATE {collation}")

    def drop_schema(self, schema: str) -> None:
        if not is_droppable(self.db.name, schema):
            raise ValueError(f"refusing to drop schema {schema}")
        with _granted(schema), self.engine.begin() as conn:
            conn.exec_driver_sql(f"DROP DATABASE IF EXISTS {quote(schema)}")

    def load_dump(self, path: Path, schema: str) -> None:
        with defaults_file(self.db) as credentials, tempfile.TemporaryFile() as stderr:
            # --one-database: statements for any other database are skipped; --sandbox (MariaDB): no shell or file
            # commands of the client
            command = [self.tools.client, f"--defaults-extra-file={credentials}", "--default-character-set=utf8mb4",
                       "--one-database", *(["--sandbox"] if self.tools.product == "mariadb" else []), schema]
            process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=stderr,
                                       env=child_env())
            timed_out = threading.Event()

            def kill():
                timed_out.set()
                process.kill()

            timer = threading.Timer(self.timeout_seconds, kill)
            timer.start()
            unsafe = None
            try:
                with gzip.open(path, "rb") as dump:
                    lines = _CheckedLines()
                    for block in iter(lambda: dump.read(CHUNK), b""):
                        process.stdin.write(lines.take(block))
                        if lines.unsafe is not None:
                            break
                    else:
                        process.stdin.write(lines.rest())
                    unsafe = lines.unsafe
                if unsafe is not None:
                    process.kill()
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
            if unsafe is not None:
                _logger.error(f"Backups: {path.parent.name} leaves the schema it is loaded into ({unsafe!r}): refused")
                raise BackupFailure("unsafe_dump")
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

    def carry_over(self, outgoing: str, incoming: str, now: datetime) -> CarryOverResult:
        with self.engine.begin() as conn:
            return carry_over(conn, outgoing, incoming, now)

    def user_differences(self, outgoing: str, incoming: str) -> list:
        with self.engine.connect() as conn:
            return user_state_differences(conn, outgoing, incoming)

    def _next_ids(self, conn, schema: str) -> dict:
        """The next id of every table with an auto-increment column: AUTO_INCREMENT, at least MAX(column) + 1"""
        result = {}
        for table, auto, column in conn.execute(AUTO_INCREMENTS, {"schema": schema}).all():
            highest = conn.exec_driver_sql(f"SELECT MAX({quote(column)}) FROM {quote(schema)}.{quote(table)}").scalar()
            result[table] = max(int(auto or 1), int(highest or 0) + 1)
        return result

    def bump_auto_increment(self, outgoing: str, incoming: str) -> int:
        with self.engine.begin() as conn:
            statements = raise_statements(incoming, self._next_ids(conn, outgoing), self._next_ids(conn, incoming))
            for statement in statements:
                conn.exec_driver_sql(statement)
        return len(statements)

    def swap(self, db: str, live: list[str], incoming: list[str], source: str, keep_as: str) -> None:
        with self.engine.begin() as conn:
            # wait at most lock_wait_seconds (a minute) for the metadata locks (the default is a day): a stuck
            # connection fails the swap instead of holding it while the operator gives up on the command
            conn.exec_driver_sql(f"SET SESSION lock_wait_timeout = {self.lock_wait_seconds}")
            conn.exec_driver_sql(rename_statement(db, live, incoming, source, keep_as))

    def audit(self, schema: str, action: str, details: dict) -> None:
        """The restore's audit row, a raw INSERT into the swapped-in schema: it may be an older backup, so event,
        category and params are written only when its audit_logs has those columns; details stays for an old one"""
        event = RESTORE_EVENTS[action]
        values = {"action": action, "resource_type": AUDIT_RESOURCE, "success": True,
                  "event": event.code, "category": event.category.value, "params": details,
                  "created_at": utcnow().replace(tzinfo=None)}
        with self.engine.begin() as conn:
            table = _reflect(conn, "audit_logs", schema)
            if table is None:
                _logger.warning(f"Backups: {schema} has no audit_logs; the {action} audit row is not written")
                return
            if "params" not in table.c:
                values["details"] = details
            row = _only(table, values)
            for name in ("params", "details"):
                # MariaDB reflects JSON as LONGTEXT: a dict would reach the driver raw
                if name in row and not isinstance(table.c[name].type, sa.JSON):
                    row[name] = json.dumps(row[name])
            conn.execute(sa.insert(table).values(**row))

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
        return folder, require_dump(folder, read_manifest(folder))
    except ManifestError as exc:
        _logger.error(f"Backups: {ref}: {exc.reason}")
        raise RestoreRefused("not_found")


def _checked(cfg: BackupConfig, ref: str, db: DatabaseConfig) -> tuple[Path, Manifest]:
    folder, manifest = _find(cfg, ref)
    if manifest.database != db.name:
        _logger.error(f"Backups: {manifest.id} is a backup of {manifest.database}, not of {db.name}")
        raise RestoreRefused("other_database")
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


# The staging copy needs about the size of the database again, plus some room
SPACE_FACTOR = 1.2


def _check_space(cfg: BackupConfig, backend: RestoreBackend, db: DatabaseConfig, skip: bool) -> None:
    """Refuse a load the MariaDB datadir has no room for: a full datadir stops the live server too"""
    if skip:
        _logger.warning("Backups: free space in the MariaDB datadir not checked (--skip-space-check)")
        return
    if not cfg.datadir:
        raise RestoreRefused("space_unknown")
    try:
        free = shutil.disk_usage(cfg.datadir).free
    except OSError as exc:
        _logger.error(f"Backups: datadir {cfg.datadir} not usable for the space check ({type(exc).__name__})")
        raise RestoreRefused("space_unknown")
    need = int(backend.schema_size(db.name) * SPACE_FACTOR) + cfg.min_free_mb * 2 ** 20
    if free < need:
        _logger.error(f"Backups: {free} bytes free in the MariaDB datadir, {need} needed for the copy")
        raise RestoreRefused("no_space")


def plan_restore(cfg: BackupConfig, db: DatabaseConfig, ref: str, *, backend: RestoreBackend) -> RestorePlan:
    _, manifest = _checked(cfg, ref, db)
    return RestorePlan(backup_id=manifest.id, label=manifest.label.value, started_at=manifest.started_at,
                       backup_revision=manifest.migration_revision, live_revision=backend.revision(db.name),
                       other_connections=backend.other_connections(db.name))


def _carry(backend: RestoreBackend, db: str, incoming: str, now: datetime) -> CarryOverResult:
    try:
        return backend.carry_over(db, incoming, now)
    except CarryOverUnsupported as exc:
        _logger.error(f"Backups: cannot carry the final state over: {exc}")
        raise RestoreRefused("backup_too_old")


def _merge(first: CarryOverResult, second: CarryOverResult) -> CarryOverResult:
    """The two carry-over passes as one result"""
    return CarryOverResult(revocations=first.revocations + second.revocations,
                           certificates_added=first.certificates_added + second.certificates_added,
                           users_added=first.users_added + second.users_added,
                           crl_number=max(first.crl_number, second.crl_number),
                           sessions_ended=first.sessions_ended + second.sessions_ended)


def _free_kept_name(backend: RestoreBackend, db: str, now: Callable[[], datetime]) -> str:
    """kept_name of now, waiting a second when a kept schema of this second exists (names are per second)"""
    existing = {kept.name for kept in backend.kept_schemas(db)}
    for _ in range(3):
        name = kept_name(db, now())
        if name not in existing:
            return name
        time.sleep(1)
    raise BackupFailure("busy")


def _drop_if_empty(backend: RestoreBackend, schema: str) -> None:
    """Drop a kept schema this run created, only while it holds no table: a swap that failed on the client may have
    completed on the server, and the kept schema then holds the only copy of the replaced data"""
    if not backend.list_tables(schema):
        backend.drop_schema(schema)
    else:
        _logger.error(f"Backups: {schema} holds tables after a failed swap; left in place (check, then undo-restore)")


def _details(carry: CarryOverResult, keep_as: str, ids_raised: int) -> dict:
    return {"kept_schema": keep_as, "revocations": carry.revocations, "certificates_added": carry.certificates_added,
            "users_added": len(carry.users_added), "crl_number": carry.crl_number,
            "sessions_ended": carry.sessions_ended, "ids_raised": ids_raised}


def restore_backup(cfg: BackupConfig, db: DatabaseConfig, ref: str, *, backend: RestoreBackend,
                   force_connections: bool = False, skip_space_check: bool = False,
                   now: Callable[[], datetime] = utcnow) -> RestoreResult:
    with writer_lock(Path(cfg.dir)):
        folder, manifest = _checked(cfg, ref, db)
        _guard(backend, db.name, force_connections)
        _check_space(cfg, backend, db, skip_space_check)
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
            carry = _carry(backend, db.name, staging, now())
            differences = backend.user_differences(db.name, staging)
            # The load took minutes: a client that connected meanwhile may have revoked a certificate. Check again and
            # carry over again (idempotent) right before the swap, so the window is the swap itself
            if not force_connections and backend.other_connections(db.name) > 0:
                raise RestoreRefused("connections")
            carry = _merge(carry, _carry(backend, db.name, staging, now()))
            ids_raised = backend.bump_auto_increment(db.name, staging)
            name = _free_kept_name(backend, db.name, now)
            backend.create_schema(name)
            keep_as = name      # only a schema this run created is ever dropped below
            backend.swap(db.name, backend.list_tables(db.name), tables, staging, keep_as)
            swapped = True
        finally:
            if not swapped:
                backend.drop_schema(staging)
                if keep_as is not None:
                    _drop_if_empty(backend, keep_as)
        backend.drop_schema(staging)
        backend.audit(db.name, "restore", {"backup_id": manifest.id, "label": manifest.label.value,
                                           "backup_revision": manifest.migration_revision,
                                           **_details(carry, keep_as, ids_raised)})
        _logger.info(f"Backups: restored {manifest.id}; the replaced tables are kept in {keep_as}")
        return RestoreResult(kept_schema=keep_as, carry=carry, user_differences=differences, ids_raised=ids_raised)


def undo_restore(cfg: BackupConfig, db: DatabaseConfig, schema: Optional[str], *, backend: RestoreBackend,
                 force_connections: bool = False,
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
        carry = _carry(backend, db.name, source, now())
        differences = backend.user_differences(db.name, source)
        ids_raised = backend.bump_auto_increment(db.name, source)
        keep_as = _free_kept_name(backend, db.name, now)
        backend.create_schema(keep_as)
        try:
            backend.swap(db.name, backend.list_tables(db.name), backend.list_tables(source), source, keep_as)
        except BaseException:
            _drop_if_empty(backend, keep_as)
            raise
        backend.drop_schema(source)
        backend.audit(db.name, "undo_restore", {"source_schema": source, **_details(carry, keep_as, ids_raised)})
        _logger.info(f"Backups: swapped {source} back in; the replaced tables are kept in {keep_as}")
        return RestoreResult(kept_schema=keep_as, carry=carry, user_differences=differences, ids_raised=ids_raised)


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
                  backend: Optional[RestoreBackend] = None, skip_space_check: bool = False) -> VerifyResult:
    folder, manifest = _find(cfg, ref)
    path = folder / manifest.file
    checksum_ok = _sha256_of(path) == manifest.sha256
    gzip_ok = _gzip_ok(path)
    if not deep or not (checksum_ok and gzip_ok):
        return VerifyResult(backup_id=manifest.id, checksum_ok=checksum_ok, gzip_ok=gzip_ok, tables_ok=None,
                            revision_ok=None)
    scratch = verify_name(db.name)
    _check_space(cfg, backend, db, skip_space_check)
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
