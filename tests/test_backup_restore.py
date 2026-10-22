"""Backups: restore, undo and deep verify (spec section 4). The database steps run against a fake backend that
records them; the SQL of the swap is checked as text; the real backend runs live on MariaDB (BACKUPS.md)."""
import gzip
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.backend.config import BackupConfig, DatabaseConfig
from app.backend.enums.general import BackupLabel
from app.backend.services.backups.carry_over import CarryOverResult, CarryOverUnsupported
from app.backend.services.backups.failures import BackupFailure
from app.backend.services.backups.locking import writer_lock
from app.backend.services.backups.manifest import FORMAT_VERSION, Manifest, backup_id, write_manifest
from app.backend.services.backups.restore import (
    RestoreRefused, code_head, known_revisions, plan_restore, prune_kept_schemas, restore_backup, undo_restore,
    verify_backup,
)
from app.backend.services.backups.schemas import KeptSchema, kept_name, parse_kept, raise_statements, rename_statement

UTC = timezone.utc
NOW = datetime(2026, 10, 22, 12, 0, 0, tzinfo=UTC)
DB = DatabaseConfig(name="db", user="u", password="p")
TABLES = ("alembic_version", "users")
REVISION = "028_no_actor_foreign_keys"
CARRY = CarryOverResult(revocations=1, certificates_added=2, users_added=((9, "u9"),), crl_number=7, sessions_ended=3)


class FakeBackend:
    def __init__(self, live_tables=("alembic_version", "users", "later_table"), connections=0, unsupported=(),
                 kept=(), fail_on=None, staging_tables=TABLES, staging_revision=REVISION):
        self.calls = []
        self.live_tables = list(live_tables)
        self.connections = connections
        self.unsupported = list(unsupported)
        self.kept = list(kept)
        self.fail_on = fail_on
        self.staging_tables = list(staging_tables)
        self.staging_revision = staging_revision
        self.audits = []
        self.tables_of = {}          # schema -> tables, overriding the defaults (e.g. a kept schema after a swap)
        self.existing = set()        # schemas create_schema refuses (already there)

    def _call(self, name, *args):
        self.calls.append((name, *args))
        if self.fail_on == name:
            raise RuntimeError(f"{name} failed")

    def unsupported_objects(self, schema):
        self._call("unsupported_objects", schema)
        return self.unsupported

    def other_connections(self, schema):
        self._call("other_connections", schema)
        return self.connections

    def create_schema(self, schema):
        self._call("create_schema", schema)
        if schema in self.existing:
            raise RuntimeError(f"{schema} exists")

    def drop_schema(self, schema):
        self._call("drop_schema", schema)

    def load_dump(self, path, schema):
        self._call("load_dump", Path(path).name, schema)

    def list_tables(self, schema):
        self._call("list_tables", schema)
        if schema in self.tables_of:
            return self.tables_of[schema]
        if schema.startswith("db_before_"):
            return []        # a kept schema created by this run, before any swap
        return self.live_tables if schema == "db" else self.staging_tables

    def revision(self, schema):
        self._call("revision", schema)
        return REVISION if schema == "db" else self.staging_revision

    def carry_over(self, outgoing, incoming, now):
        self._call("carry_over", outgoing, incoming)
        if self.fail_on == "too_old":
            raise CarryOverUnsupported("user_certificates")
        return CARRY

    def user_differences(self, outgoing, incoming):
        self._call("user_differences", outgoing, incoming)
        return [(1, "u1", "is_active True->False")]

    def bump_auto_increment(self, outgoing, incoming):
        self._call("bump_auto_increment", outgoing, incoming)
        return 4

    def swap(self, db, live, incoming, source, keep_as):
        self._call("swap", tuple(live), tuple(incoming), source, keep_as)

    def audit(self, schema, action, details):
        self._call("audit", schema, action)
        self.audits.append(details)

    def kept_schemas(self, db):
        self._call("kept_schemas", db)
        return self.kept


def put_backup(directory: Path, revision=REVISION, body=b"-- dump\n") -> Manifest:
    started = datetime(2026, 10, 21, 3, 15, 0, tzinfo=UTC)
    data = gzip.compress(body)
    manifest = Manifest(
        format_version=FORMAT_VERSION, id=backup_id(started, BackupLabel.SCHEDULED), label=BackupLabel.SCHEDULED,
        started_at=started, finished_at=started, database="db", server_product="mariadb", server_version="11.8",
        dump_tool="d", migration_revision=revision, tables=TABLES, file="dump.sql.gz", size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )
    folder = directory / manifest.id
    folder.mkdir(parents=True)
    (folder / "dump.sql.gz").write_bytes(data)
    write_manifest(folder, manifest)
    return manifest


@pytest.fixture
def cfg(tmp_path):
    return BackupConfig(dir=str(tmp_path / "backups"))


def restore(cfg, backend, ref="latest", **kw):
    return restore_backup(cfg, DB, ref, backend=backend, now=lambda: NOW, **kw)


def names(backend):
    return [call[0] for call in backend.calls]


def test_kept_name_and_parse():
    assert kept_name("db", NOW) == "db_before_20261022_120000"
    rows = [("db_before_20261022_120000", 10), ("db_before_x", 1), ("db_restore", 5), ("dbx_before_20261022_120000", 1)]
    assert parse_kept("db", rows) == [KeptSchema(name="db_before_20261022_120000", created_at=NOW, size_bytes=10)]


def test_rename_statement():
    assert rename_statement("db", ["a", "b"], ["a", "c"], "db_restore", "db_before_x") == (
        "RENAME TABLE `db`.`a` TO `db_before_x`.`a`, `db`.`b` TO `db_before_x`.`b`, "
        "`db_restore`.`a` TO `db`.`a`, `db_restore`.`c` TO `db`.`c`")
    assert rename_statement("db", ["we`ird"], [], "s", "k") == "RENAME TABLE `db`.`we``ird` TO `k`.`we``ird`"


def test_restore_order(cfg):
    manifest = put_backup(Path(cfg.dir))
    backend = FakeBackend()
    result = restore(cfg, backend)
    assert names(backend) == [
        "unsupported_objects", "other_connections", "drop_schema", "create_schema", "load_dump", "list_tables",
        "revision", "carry_over", "user_differences", "bump_auto_increment", "create_schema", "list_tables", "swap",
        "drop_schema", "audit",
    ]
    calls = dict((c[0], c[1:]) for c in backend.calls if c[0] in ("load_dump", "carry_over", "swap", "audit"))
    assert calls["load_dump"] == ("dump.sql.gz", "db_restore")
    assert ("bump_auto_increment", "db", "db_restore") in backend.calls
    assert result.ids_raised == 4
    assert calls["carry_over"] == ("db", "db_restore")
    assert calls["swap"] == (("alembic_version", "users", "later_table"), TABLES, "db_restore",
                             "db_before_20261022_120000")
    assert calls["audit"] == ("db", "restore")
    assert backend.audits[0] == {
        "backup_id": manifest.id, "label": "scheduled", "backup_revision": REVISION,
        "kept_schema": "db_before_20261022_120000", "revocations": 1, "certificates_added": 2,
        "users_added": 1, "crl_number": 7, "sessions_ended": 3, "ids_raised": 4,
    }
    assert result.kept_schema == "db_before_20261022_120000"
    assert result.carry == CARRY
    assert result.user_differences == [(1, "u1", "is_active True->False")]


def test_refuses_with_connections(cfg):
    put_backup(Path(cfg.dir))
    backend = FakeBackend(connections=2)
    with pytest.raises(RestoreRefused) as exc:
        restore(cfg, backend)
    assert exc.value.code == "connections"
    assert names(backend) == ["unsupported_objects", "other_connections"]
    restore(cfg, FakeBackend(connections=2), force_connections=True)


def test_refuses_unsupported_objects(cfg):
    put_backup(Path(cfg.dir))
    backend = FakeBackend(unsupported=["view v"])
    with pytest.raises(RestoreRefused) as exc:
        restore(cfg, backend)
    assert exc.value.code == "unsupported_objects"
    assert "create_schema" not in names(backend)


def test_refuses_unknown_revision(cfg):
    put_backup(Path(cfg.dir), revision="999_future")
    backend = FakeBackend()
    with pytest.raises(RestoreRefused) as exc:
        restore(cfg, backend)
    assert exc.value.code == "unknown_revision"
    assert backend.calls == []


def test_unknown_backup(cfg):
    Path(cfg.dir).mkdir()
    with pytest.raises(RestoreRefused) as exc:
        restore(cfg, FakeBackend(), ref="20990101T000000Z-manual")
    assert exc.value.code == "not_found"


def test_checksum_mismatch(cfg):
    manifest = put_backup(Path(cfg.dir))
    (Path(cfg.dir) / manifest.id / "dump.sql.gz").write_bytes(gzip.compress(b"other"))
    backend = FakeBackend()
    with pytest.raises(RestoreRefused) as exc:
        restore(cfg, backend)
    assert exc.value.code == "checksum_mismatch"
    assert backend.calls == []


def test_failure_after_load_drops_staging(cfg):
    put_backup(Path(cfg.dir))
    backend = FakeBackend(fail_on="swap")
    with pytest.raises(RuntimeError):
        restore(cfg, backend)
    assert backend.calls[-4:] == [backend.calls[-4], ("drop_schema", "db_restore"),
                                  ("list_tables", "db_before_20261022_120000"),
                                  ("drop_schema", "db_before_20261022_120000")]
    assert backend.calls[-4][0] == "swap"
    assert "audit" not in names(backend)


def test_loaded_tables_must_match_the_manifest(cfg):
    put_backup(Path(cfg.dir))
    backend = FakeBackend(staging_tables=("users",))
    with pytest.raises(BackupFailure) as exc:
        restore(cfg, backend)
    assert exc.value.code == "load_failed"
    assert "carry_over" not in names(backend)
    assert backend.calls[-1] == ("drop_schema", "db_restore")


def test_loaded_revision_must_match_the_manifest(cfg):
    put_backup(Path(cfg.dir))
    backend = FakeBackend(staging_revision="027_scope_group_kinds")
    with pytest.raises(BackupFailure) as exc:
        restore(cfg, backend)
    assert exc.value.code == "load_failed"


def test_restore_holds_the_lock(cfg):
    put_backup(Path(cfg.dir))
    backend = FakeBackend()
    with writer_lock(Path(cfg.dir)):
        with pytest.raises(BackupFailure) as exc:
            restore(cfg, backend)
    assert exc.value.code == "busy" and backend.calls == []


def test_plan_restore(cfg):
    manifest = put_backup(Path(cfg.dir))
    plan = plan_restore(cfg, DB, "latest", backend=FakeBackend(connections=1))
    assert (plan.backup_id, plan.label, plan.backup_revision, plan.live_revision, plan.other_connections) == (
        manifest.id, "scheduled", REVISION, REVISION, 1)


OLDER = KeptSchema(name="db_before_20261020_100000", created_at=datetime(2026, 10, 20, 10, tzinfo=UTC), size_bytes=1)
NEWER = KeptSchema(name="db_before_20261021_100000", created_at=datetime(2026, 10, 21, 10, tzinfo=UTC), size_bytes=1)


def test_undo_picks_newest_kept(cfg):
    Path(cfg.dir).mkdir()
    backend = FakeBackend(kept=[OLDER, NEWER])
    result = undo_restore(cfg, DB, None, backend=backend, now=lambda: NOW)
    swap = next(c for c in backend.calls if c[0] == "swap")
    assert swap[3:] == (NEWER.name, "db_before_20261022_120000")
    assert ("carry_over", "db", NEWER.name) in backend.calls
    order = names(backend)
    assert order.index("carry_over") < order.index("bump_auto_increment") < order.index("swap")
    assert ("bump_auto_increment", "db", NEWER.name) in backend.calls
    assert backend.calls[-2:] == [("drop_schema", NEWER.name), ("audit", "db", "undo_restore")]
    assert backend.audits[0]["source_schema"] == NEWER.name
    assert result.kept_schema == "db_before_20261022_120000"
    named = FakeBackend(kept=[OLDER, NEWER])
    undo_restore(cfg, DB, OLDER.name, backend=named, now=lambda: NOW)
    assert next(c for c in named.calls if c[0] == "swap")[3] == OLDER.name


def test_undo_with_nothing_kept(cfg):
    Path(cfg.dir).mkdir()
    with pytest.raises(RestoreRefused) as exc:
        undo_restore(cfg, DB, None, backend=FakeBackend(), now=lambda: NOW)
    assert exc.value.code == "nothing_to_undo"
    with pytest.raises(RestoreRefused) as exc:
        undo_restore(cfg, DB, "db_before_19990101_000000", backend=FakeBackend(kept=[NEWER]), now=lambda: NOW)
    assert exc.value.code == "nothing_to_undo"


def test_verify_shallow(cfg):
    put_backup(Path(cfg.dir))
    backend = FakeBackend()
    result = verify_backup(cfg, DB, "latest", deep=False, backend=backend)
    assert (result.checksum_ok, result.gzip_ok, result.tables_ok, result.revision_ok) == (True, True, None, None)
    assert backend.calls == []


def test_verify_detects_a_broken_gzip(cfg):
    manifest = put_backup(Path(cfg.dir))
    path = Path(cfg.dir) / manifest.id / "dump.sql.gz"
    broken = path.read_bytes()[:-6]
    path.write_bytes(broken)
    result = verify_backup(cfg, DB, manifest.id, deep=True, backend=FakeBackend())
    assert result.checksum_ok is False and result.gzip_ok is False and result.tables_ok is None


def test_verify_deep(cfg):
    put_backup(Path(cfg.dir))
    backend = FakeBackend()
    result = verify_backup(cfg, DB, "latest", deep=True, backend=backend)
    assert (result.checksum_ok, result.gzip_ok, result.tables_ok, result.revision_ok) == (True, True, True, True)
    assert backend.calls[0] == ("drop_schema", "db_verify") and backend.calls[-1] == ("drop_schema", "db_verify")
    missing = FakeBackend(staging_tables=("users",))
    assert verify_backup(cfg, DB, "latest", deep=True, backend=missing).tables_ok is False
    assert missing.calls[-1] == ("drop_schema", "db_verify")


def test_known_revisions_has_head():
    assert code_head() in known_revisions()
    assert REVISION in known_revisions() and "001_relativize_media_paths" in known_revisions()


def test_prune_kept_schemas(cfg):
    Path(cfg.dir).mkdir()
    now = datetime(2026, 10, 28, 10, 0, 1, tzinfo=UTC)   # OLDER is 8 days old, NEWER 7 days and 1 second
    backend = FakeBackend(kept=[OLDER, NEWER])
    assert prune_kept_schemas(cfg, DB, backend=backend, now=now, dry_run=True) == [OLDER.name, NEWER.name]
    assert "drop_schema" not in names(backend)
    assert prune_kept_schemas(BackupConfig(dir=cfg.dir, keep_previous_days=8), DB, backend=backend, now=now) == \
        [OLDER.name]
    assert backend.calls[-1] == ("drop_schema", OLDER.name)


def test_backup_too_old_for_the_carry_over(cfg):
    put_backup(Path(cfg.dir))
    backend = FakeBackend(fail_on="too_old")
    with pytest.raises(RestoreRefused) as exc:
        restore(cfg, backend)
    assert exc.value.code == "backup_too_old"
    assert backend.calls[-1] == ("drop_schema", "db_restore")


def test_kept_schema_survives_a_swap_error_after_the_server_renamed(cfg):
    """The RENAME may complete on the server although the client got an error (lost connection, Ctrl-C while it
    waited on a lock): the kept schema then holds the only copy of the replaced data and must not be dropped"""
    put_backup(Path(cfg.dir))
    backend = FakeBackend(fail_on="swap")
    backend.tables_of["db_before_20261022_120000"] = ["alembic_version", "users", "later_table"]
    with pytest.raises(RuntimeError):
        restore(cfg, backend)
    assert ("drop_schema", "db_before_20261022_120000") not in backend.calls


def test_an_existing_kept_schema_is_never_dropped(cfg):
    put_backup(Path(cfg.dir))
    backend = FakeBackend()
    backend.existing.add("db_before_20261022_120000")
    with pytest.raises(RuntimeError):
        restore(cfg, backend)
    assert ("drop_schema", "db_before_20261022_120000") not in backend.calls
    assert backend.calls[-1] == ("drop_schema", "db_restore")


def test_undo_keeps_both_schemas_on_a_swap_error_after_the_server_renamed(cfg):
    Path(cfg.dir).mkdir()
    backend = FakeBackend(kept=[NEWER], fail_on="swap")
    backend.tables_of["db_before_20261022_120000"] = ["users"]
    with pytest.raises(RuntimeError):
        undo_restore(cfg, DB, None, backend=backend, now=lambda: NOW)
    assert not any(c[0] == "drop_schema" for c in backend.calls)


def test_raise_statements():
    """Only tables both sides have, only where the outgoing side is further on (spec 8.1)"""
    assert raise_statements("db_restore", {"a": 10, "b": 5, "only_out": 3, "we`ird": 7},
                            {"a": 4, "b": 9, "only_in": 2, "we`ird": 1}) == [
        "ALTER TABLE `db_restore`.`a` AUTO_INCREMENT = 10",
        "ALTER TABLE `db_restore`.`we``ird` AUTO_INCREMENT = 7",
    ]
