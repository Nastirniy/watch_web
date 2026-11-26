"""Backups against a real MariaDB (edge test plan, environment E2): the real MariaDbBackend, mariadb-dump and the
mariadb client on a throwaway database radio_monitoring_edge (the live database is never touched).

Run on a server with MariaDB, in a one-off container of the image (docs/superpowers/plans/
2026-10-22-db-backups-edge-test-plan.md): WW_TEST_MARIADB=1, the MYSETTINGS_DATABASE__* of .env, optionally
WW_TEST_DATADIR (the datadir mounted read-only). Skipped everywhere else.
"""
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import OperationalError
from sqlmodel import SQLModel

pytestmark = pytest.mark.skipif(os.environ.get("WW_TEST_MARIADB") != "1", reason="needs a MariaDB server (E2)")

EDGE = "radio_monitoring_edge"
UTC = timezone.utc
BIG_ROWS = 200_000


def _db():
    from app.backend.config import DatabaseConfig, settings
    return DatabaseConfig(**{**settings.database.model_dump(), "name": EDGE})


def _engine(name: str = EDGE):
    from app.backend.services.backups.restore import sync_engine
    from app.backend.config import DatabaseConfig, settings
    return sync_engine(DatabaseConfig(**{**settings.database.model_dump(), "name": name}))


def q(sql: str, **params):
    engine = _engine()
    try:
        with engine.begin() as conn:
            result = conn.execute(sa.text(sql), params)
            return result.all() if result.returns_rows else result.rowcount
    finally:
        engine.dispose()


@pytest.fixture
def edge(tmp_path):
    """A fresh radio_monitoring_edge from the models, stamped at the head, a few rows; dropped afterwards"""
    import app.backend.models  # noqa: F401
    from app.backend.config import settings
    from app.backend.services.backups.restore import code_head
    server = _engine(settings.database.name)
    with server.begin() as conn:
        for schema in conn.execute(sa.text("SHOW DATABASES LIKE 'radio\\_monitoring\\_edge%'")).scalars():
            conn.exec_driver_sql(f"DROP DATABASE `{schema}`")
        conn.exec_driver_sql(f"CREATE DATABASE `{EDGE}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    engine = _engine()
    with engine.begin() as conn:
        SQLModel.metadata.create_all(conn)
        conn.exec_driver_sql("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY)")
        conn.execute(sa.text("INSERT INTO alembic_version VALUES (:v)"), {"v": code_head()})
        conn.exec_driver_sql(
            "INSERT INTO users (id, username, username_alias, is_active, user_type, is_deleted, created_at, updated_at) "
            "VALUES (1, 'ivan', 'Іван', 1, 'admin', 0, UTC_TIMESTAMP(), UTC_TIMESTAMP())")
    engine.dispose()
    yield _db()
    with server.begin() as conn:
        for schema in conn.execute(sa.text("SHOW DATABASES LIKE 'radio\\_monitoring\\_edge%'")).scalars():
            conn.exec_driver_sql(f"DROP DATABASE `{schema}`")
    server.dispose()


@pytest.fixture
def cfg(tmp_path):
    from app.backend.config import BackupConfig
    return BackupConfig(dir=str(tmp_path / "backups"), datadir=os.environ.get("WW_TEST_DATADIR", ""), min_free_mb=0)


@pytest.fixture
def backend(edge):
    from app.backend.services.backups.client_tools import find_tools
    from app.backend.services.backups.restore import MariaDbBackend
    return MariaDbBackend(edge, find_tools(), timeout_seconds=600, lock_wait_seconds=3)


def make_backup(cfg, edge, backend, label=None):
    from app.backend.enums.general import BackupLabel
    from app.backend.services.backups.dump import create_backup
    return create_backup(cfg, edge, label or BackupLabel.MANUAL,
                         object_guard=lambda: backend.unsupported_objects(edge.name)).manifest


def restore(cfg, edge, backend, ref="latest", **kw):
    from app.backend.services.backups.restore import restore_backup
    kw.setdefault("skip_space_check", not cfg.datadir)
    return restore_backup(cfg, edge, ref, backend=backend, **kw)


def checksums(schema: str, tables) -> dict:
    return {t: q(f"CHECKSUM TABLE `{schema}`.`{t}`")[0][1] for t in tables}


# Tables the restore itself writes (carry-over, audit row): left out of the byte-for-byte comparison
WRITTEN_BY_RESTORE = {"user_sessions", "user_certificates", "certificate_authority_state", "audit_logs", "users"}


def test_c1_round_trip_is_byte_exact(cfg, edge, backend):
    """C1: utf8mb4 (Cyrillic, emoji), binary, NULL, long strings and datetime limits survive dump and load"""
    q("CREATE TABLE edge_types (id INT PRIMARY KEY, t LONGTEXT, b LONGBLOB, d DATETIME(6), n DECIMAL(30,10), "
      "j JSON NULL, e VARCHAR(255)) ENGINE=InnoDB")
    rows = [
        (1, "Привіт, світ", bytes(range(256)), "1000-01-01 00:00:00.000000", "-99999999999999999999.9999999999",
         '{"k": "значення \\" \\\\ \\n"}', "😀🚀 ⅀ 𝄞"),
        (2, "x" * 1_000_000, b"\x00" * 70_000, "9999-12-31 23:59:59.999999", "0.0000000001", None, ""),
        (3, None, None, None, None, None, None),
        (4, "line1\nline2\r\n\tUSE radio_monitoring;\n\\! rm -rf /", b"\n\\USE x;", "2026-10-22 12:00:00", "1", "[]",
         "'quote' `tick` \"dq\""),
    ]
    for row in rows:
        q("INSERT INTO edge_types VALUES (:id, :t, :b, :d, :n, :j, :e)",
          **dict(zip(("id", "t", "b", "d", "n", "j", "e"), row)))
    before = checksums(EDGE, ["edge_types", "alembic_version"])
    make_backup(cfg, edge, backend)
    q("DELETE FROM edge_types WHERE id = 1")
    result = restore(cfg, edge, backend)
    assert checksums(EDGE, ["edge_types", "alembic_version"]) == before
    assert checksums(result.kept_schema, ["alembic_version"]) == {"alembic_version": before["alembic_version"]}


def test_c2_dump_is_consistent_under_writes(cfg, edge, backend):
    """C2: parents and children inserted in transactions during the dump: the backup never has a child alone"""
    q("CREATE TABLE edge_parent (id INT AUTO_INCREMENT PRIMARY KEY, v INT) ENGINE=InnoDB")
    q("CREATE TABLE edge_child (id INT AUTO_INCREMENT PRIMARY KEY, parent_id INT NOT NULL, "
      "FOREIGN KEY (parent_id) REFERENCES edge_parent(id)) ENGINE=InnoDB")
    q("CREATE TABLE edge_bulk (id INT AUTO_INCREMENT PRIMARY KEY, pad CHAR(200)) ENGINE=InnoDB")
    q(f"INSERT INTO edge_bulk (pad) SELECT REPEAT('x', 200) FROM seq_1_to_{BIG_ROWS}")
    stop = threading.Event()

    def writer():
        engine = _engine()
        while not stop.is_set():
            with engine.begin() as conn:
                parent = conn.execute(sa.text("INSERT INTO edge_parent (v) VALUES (1)")).lastrowid
                conn.execute(sa.text("INSERT INTO edge_child (parent_id) VALUES (:p)"), {"p": parent})
                conn.execute(sa.text("INSERT INTO edge_child (parent_id) VALUES (:p)"), {"p": parent})
        engine.dispose()

    threads = [threading.Thread(target=writer) for _ in range(4)]
    for thread in threads:
        thread.start()
    try:
        time.sleep(0.5)
        make_backup(cfg, edge, backend)
    finally:
        stop.set()
        for thread in threads:
            thread.join()
    result = restore(cfg, edge, backend)
    assert q("SELECT COUNT(*) FROM edge_child c LEFT JOIN edge_parent p ON p.id = c.parent_id WHERE p.id IS NULL")[0][0] == 0
    parents, children = q("SELECT (SELECT COUNT(*) FROM edge_parent), (SELECT COUNT(*) FROM edge_child)")[0]
    assert children == 2 * parents and parents > 0
    assert result.kept_schema


def test_c3_ddl_during_the_dump_never_gives_a_broken_backup(cfg, edge, backend):
    """C3: an ALTER from another session during the dump: the dump fails cleanly or the backup loads"""
    from app.backend.services.backups.failures import BackupFailure
    from app.backend.services.backups.restore import verify_backup
    q("CREATE TABLE zz_edge_late (id INT AUTO_INCREMENT PRIMARY KEY, pad CHAR(200)) ENGINE=InnoDB")
    q(f"INSERT INTO zz_edge_late (pad) SELECT REPEAT('y', 200) FROM seq_1_to_{BIG_ROWS}")
    q("CREATE TABLE edge_bulk (id INT AUTO_INCREMENT PRIMARY KEY, pad CHAR(200)) ENGINE=InnoDB")
    q(f"INSERT INTO edge_bulk (pad) SELECT REPEAT('x', 200) FROM seq_1_to_{BIG_ROWS}")
    alter = threading.Thread(target=lambda: (time.sleep(0.3), q("ALTER TABLE zz_edge_late ADD COLUMN extra INT NULL")))
    alter.start()
    try:
        manifest = make_backup(cfg, edge, backend)
    except BackupFailure as exc:
        assert exc.code == "dump_failed"
        assert not [p for p in Path(cfg.dir).iterdir() if not p.name.startswith(".")]
        return
    finally:
        alter.join()
    result = verify_backup(cfg, edge, manifest.id, deep=True, backend=backend, skip_space_check=True)
    assert (result.checksum_ok, result.gzip_ok, result.tables_ok, result.revision_ok) == (True, True, True, True)


@pytest.mark.parametrize("statement, name", [
    ("CREATE VIEW edge_v AS SELECT id FROM users", "view edge_v"),
    ("CREATE TABLE edge_m (id INT) ENGINE=MyISAM", "table edge_m"),
    ("CREATE TRIGGER edge_t BEFORE INSERT ON users FOR EACH ROW SET NEW.username_alias = NEW.username_alias",
     "trigger edge_t"),
    ("CREATE PROCEDURE edge_p() SELECT 1", "procedure edge_p"),
    ("CREATE SEQUENCE edge_s", "sequence edge_s"),
])
def test_c9_unsupported_objects_refused(cfg, edge, backend, statement, name):
    from app.backend.services.backups.failures import BackupFailure
    from app.backend.services.backups.restore import RestoreRefused
    make_backup(cfg, edge, backend)
    q(statement)
    assert any(found.startswith(name) for found in backend.unsupported_objects(EDGE))
    with pytest.raises(BackupFailure) as exc:
        make_backup(cfg, edge, backend)
    assert exc.value.code == "unsupported_objects"
    with pytest.raises(RestoreRefused) as refused:
        restore(cfg, edge, backend)
    assert refused.value.code == "unsupported_objects"


def test_r2_a_dump_that_switches_database_never_writes_there(cfg, edge, backend, tmp_path):
    """R2: a backup whose dump says USE <live database> is refused and writes nothing anywhere"""
    import gzip
    import hashlib
    import json
    from app.backend.services.backups.failures import BackupFailure
    manifest = make_backup(cfg, edge, backend)
    folder = Path(cfg.dir) / manifest.id
    text = gzip.decompress((folder / "dump.sql.gz").read_bytes()).decode("utf-8")
    evil = text + f"USE `{EDGE}`;\nCREATE TABLE edge_evil_marker (id INT);\n"
    data = gzip.compress(evil.encode("utf-8"))
    (folder / "dump.sql.gz").write_bytes(data)
    meta = json.loads((folder / "manifest.json").read_text())
    meta.update(sha256=hashlib.sha256(data).hexdigest(), size_bytes=len(data))
    (folder / "manifest.json").write_text(json.dumps(meta))
    with pytest.raises(BackupFailure) as exc:
        restore(cfg, edge, backend)
    assert exc.value.code == "unsafe_dump"
    assert q("SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_NAME = 'edge_evil_marker'")[0][0] == 0
    assert q("SELECT COUNT(*) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME LIKE 'radio\\_monitoring\\_edge\\_%'")[0][0] == 0


def test_r4_a_held_metadata_lock_fails_the_swap_and_keeps_the_live_data(cfg, edge, backend):
    """R4: an open transaction on a live table: the RENAME waits lock_wait_seconds, fails, nothing is lost"""
    make_backup(cfg, edge, backend)
    q("INSERT INTO users (id, username, username_alias, is_active, user_type, is_deleted, created_at, updated_at) "
      "VALUES (2, 'after', 'A', 1, 'viewer', 0, UTC_TIMESTAMP(), UTC_TIMESTAMP())")
    holder = _engine()
    conn = holder.connect()
    trans = conn.begin()
    conn.execute(sa.text("SELECT * FROM users FOR UPDATE")).all()
    try:
        with pytest.raises(OperationalError):
            restore(cfg, edge, backend, force_connections=True)
    finally:
        trans.rollback()
        conn.close()
        holder.dispose()
    assert q("SELECT username FROM users ORDER BY id") == [("ivan",), ("after",)]
    assert q("SELECT COUNT(*) FROM information_schema.SCHEMATA WHERE SCHEMA_NAME LIKE 'radio\\_monitoring\\_edge\\_%'")[0][0] == 0


def _add_certificate(cert_id: int, user_id: int, revoked: bool = False):
    q("INSERT INTO user_certificates (id, user_id, source, label, serial, fingerprint_hash, cn_hash, cn_hash_scheme, "
      "serial_hash, ca_issued, export_count, created_at, revoked_at, revocation_reason) VALUES "
      "(:id, :u, 'issued', :l, :s, :f, 'cn', 2, :sh, 1, 0, UTC_TIMESTAMP(), :r, :rr)",
      id=cert_id, u=user_id, l=f"c{cert_id}", s=f"{cert_id:x}", f=f"fp{cert_id}", sh=f"s{cert_id}",
      r=datetime.now(UTC).replace(tzinfo=None) if revoked else None, rr="key_compromise" if revoked else None)


def test_r6_a_revocation_during_the_restore_survives(cfg, edge, backend):
    """R6: a certificate revoked after the first carry-over pass is carried by the second, right before the swap"""
    from app.backend.services.backups.restore import MariaDbBackend
    _add_certificate(10, 1)
    make_backup(cfg, edge, backend)

    class RevokingBackend(MariaDbBackend):
        def user_differences(self, outgoing, incoming):
            q("UPDATE user_certificates SET revoked_at = UTC_TIMESTAMP(), revocation_reason = 'key_compromise' "
              "WHERE id = 10")
            return super().user_differences(outgoing, incoming)

    revoking = RevokingBackend(edge, backend.tools, timeout_seconds=600, lock_wait_seconds=3)
    restore(cfg, edge, revoking)
    assert q("SELECT revocation_reason FROM user_certificates WHERE id = 10") == [("key_compromise",)]


def test_r10_placeholder_username_clash_uses_the_collation(cfg, edge, backend):
    """R10: user 1 'ivan' in the backup is renamed afterwards and a new 'IVAN' gets a certificate: the placeholder
    must not hit the case-insensitive unique index"""
    make_backup(cfg, edge, backend)
    q("UPDATE users SET username = 'petro' WHERE id = 1")
    q("INSERT INTO users (id, username, username_alias, is_active, user_type, is_deleted, created_at, updated_at) "
      "VALUES (5, 'IVAN', 'I', 1, 'viewer', 0, UTC_TIMESTAMP(), UTC_TIMESTAMP())")
    _add_certificate(20, 5)
    result = restore(cfg, edge, backend)
    assert result.carry.users_added == ((5, "IVAN~5"),)
    assert q("SELECT username, is_active, is_deleted FROM users WHERE id = 5") == [("IVAN~5", 0, 1)]
    assert q("SELECT revoked_at IS NOT NULL FROM user_certificates WHERE id = 20") == [(1,)]


def test_r11_no_id_is_handed_out_twice(cfg, edge, backend):
    """R11: rows created and deleted after the backup: the next id after the restore is beyond all of them"""
    make_backup(cfg, edge, backend)
    for n in range(2, 12):
        q("INSERT INTO users (id, username, username_alias, is_active, user_type, is_deleted, created_at, updated_at) "
          "VALUES (:id, :u, 'x', 1, 'viewer', 0, UTC_TIMESTAMP(), UTC_TIMESTAMP())", id=n, u=f"u{n}")
    q("DELETE FROM users WHERE id > 5")
    q("INSERT INTO users (username, username_alias, is_active, user_type, is_deleted, created_at, updated_at) "
      "VALUES ('big', 'x', 1, 'viewer', 0, UTC_TIMESTAMP(), UTC_TIMESTAMP())")
    highest = q("SELECT MAX(id) FROM users")[0][0]
    result = restore(cfg, edge, backend)
    assert result.ids_raised >= 1
    q("INSERT INTO users (username, username_alias, is_active, user_type, is_deleted, created_at, updated_at) "
      "VALUES ('new', 'x', 1, 'viewer', 0, UTC_TIMESTAMP(), UTC_TIMESTAMP())")
    assert q("SELECT id FROM users WHERE username = 'new'")[0][0] > highest


def test_r12_undo_chain(cfg, edge, backend):
    """R12: restore, undo, undo again: each swap brings the other state back"""
    from app.backend.services.backups.restore import undo_restore
    make_backup(cfg, edge, backend)
    q("UPDATE users SET username_alias = 'after the backup' WHERE id = 1")
    restore(cfg, edge, backend)
    assert q("SELECT username_alias FROM users WHERE id = 1") == [("Іван",)]
    time.sleep(1.1)                     # kept schema names are per second
    undo_restore(cfg, edge, None, backend=backend)
    assert q("SELECT username_alias FROM users WHERE id = 1") == [("after the backup",)]
    time.sleep(1.1)
    undo_restore(cfg, edge, None, backend=backend)
    assert q("SELECT username_alias FROM users WHERE id = 1") == [("Іван",)]



def _account(prefix: str):
    """A database account the runner created for these tests (WW_TEST_<PREFIX>_USER / _PASSWORD), or skip"""
    user, password = os.environ.get(f"WW_TEST_{prefix}_USER"), os.environ.get(f"WW_TEST_{prefix}_PASSWORD")
    if not user or password is None:
        pytest.skip(f"needs the {prefix.lower()} test account (created by the E2 runner)")
    from app.backend.config import DatabaseConfig
    return DatabaseConfig(**{**_db().model_dump(), "user": user, "password": password})


def _backend_for(db):
    from app.backend.services.backups.client_tools import find_tools
    from app.backend.services.backups.restore import MariaDbBackend
    return MariaDbBackend(db, find_tools(), timeout_seconds=600, lock_wait_seconds=3)


def _password_seen_in_processes(password: str) -> bool:
    """In the arguments or the environment of a client tool (this test process holds it on purpose)"""
    for proc in Path("/proc").iterdir():
        if not proc.name.isdigit():
            continue
        try:
            program = (proc / "cmdline").read_bytes().split(bytes(1))[0].rsplit(b"/", 1)[-1]
        except OSError:
            continue
        if program not in (b"mariadb", b"mariadb-dump"):
            continue
        for name in ("cmdline", "environ"):
            try:
                if password.encode("utf-8") in (proc / name).read_bytes():
                    return True
            except OSError:
                continue
    return False


def test_c7_a_password_with_special_characters(cfg, edge):
    """C7: quotes, backslash, #, ;, spaces, $ and backtick in the password: dump, deep verify and restore work, and
    the password never shows in a process's arguments or environment. Not Cyrillic: PyMySQL sends the password
    latin-1 encoded, so no part of the application can use one (ENV.md)"""
    from app.backend.enums.general import BackupLabel
    from app.backend.services.backups.dump import create_backup
    from app.backend.services.backups.restore import restore_backup, verify_backup
    db = _account("SPECIAL")
    special = _backend_for(db)
    q("CREATE TABLE edge_bulk (id INT AUTO_INCREMENT PRIMARY KEY, pad CHAR(200)) ENGINE=InnoDB")
    q(f"INSERT INTO edge_bulk (pad) SELECT REPEAT('x', 200) FROM seq_1_to_{BIG_ROWS}")
    seen, done = [], threading.Event()

    def watch():
        while not done.is_set():
            seen.append(_password_seen_in_processes(db.password))

    watcher = threading.Thread(target=watch)
    watcher.start()
    try:
        manifest = create_backup(cfg, db, BackupLabel.MANUAL, tools=special.tools).manifest
        result = verify_backup(cfg, db, manifest.id, deep=True, backend=special, skip_space_check=True)
    finally:
        done.set()
        watcher.join()
    assert seen and not any(seen)      # watched while the tools ran
    assert (result.tables_ok, result.revision_ok) == (True, True)
    restore_backup(cfg, db, manifest.id, backend=special, skip_space_check=True)
    assert q("SELECT COUNT(*) FROM edge_bulk")[0][0] == BIG_ROWS


def test_c8_without_a_grant_on_the_staging_schemas(cfg, edge):
    """C8: an account with rights on the database only: the backup works, a restore is refused (no_grant) before
    anything changes"""
    from app.backend.enums.general import BackupLabel
    from app.backend.services.backups.dump import create_backup
    from app.backend.services.backups.restore import RestoreRefused, restore_backup
    db = _account("NOGRANT")
    limited = _backend_for(db)
    manifest = create_backup(cfg, db, BackupLabel.MANUAL, tools=limited.tools).manifest
    q("UPDATE users SET username_alias = 'changed' WHERE id = 1")
    with pytest.raises(RestoreRefused) as exc:
        restore_backup(cfg, db, manifest.id, backend=limited, skip_space_check=True)
    assert exc.value.code == "no_grant"
    assert q("SELECT username_alias FROM users WHERE id = 1") == [("changed",)]


def test_c8_without_process_the_same_account_is_still_counted(cfg, edge):
    """C8: without PROCESS an account sees only its own connections: the api and the worker use the same account,
    so they are still counted (connections of other accounts are not: BACKUPS.md asks for PROCESS)"""
    from app.backend.enums.general import BackupLabel
    from app.backend.services.backups.dump import create_backup
    from app.backend.services.backups.restore import RestoreRefused, restore_backup, sync_engine
    db = _account("NOPROC")
    limited = _backend_for(db)
    manifest = create_backup(cfg, db, BackupLabel.MANUAL, tools=limited.tools).manifest
    same = sync_engine(db).connect()
    try:
        with pytest.raises(RestoreRefused) as exc:
            restore_backup(cfg, db, manifest.id, backend=limited, skip_space_check=True)
        assert exc.value.code == "connections"
    finally:
        same.close()
    other = _engine().connect()
    try:
        assert limited.other_connections(EDGE) == 0
    finally:
        other.close()


def _restore_audit_row(cfg, edge, backend, old_schema: bool):
    """Restore a backup whose audit_logs has (or, old_schema, lacks) the event columns; the audit row it gets"""
    if old_schema:
        for index in ("ix_audit_logs_category_created", "ix_audit_logs_event"):
            q(f"DROP INDEX `{index}` ON audit_logs")
        q("ALTER TABLE audit_logs DROP COLUMN event, DROP COLUMN category, DROP COLUMN params")
    make_backup(cfg, edge, backend)
    restore(cfg, edge, backend)
    columns = "action, resource_type, details" if old_schema else "action, resource_type, event, category, params"
    rows = q(f"SELECT {columns} FROM audit_logs WHERE action = 'restore'")
    print("AUDIT ROW", "older schema" if old_schema else "new columns", rows)
    return rows


def test_restore_audit_row_into_a_schema_with_the_event_columns(cfg, edge, backend):
    import json
    ((action, resource, event, category, params),) = _restore_audit_row(cfg, edge, backend, old_schema=False)
    assert (action, resource, event, category) == ("restore", "database", "database.restore", "audit")
    assert json.loads(params)["kept_schema"].startswith(f"{EDGE}_before_")


def test_restore_audit_row_into_an_older_schema_keeps_details(cfg, edge, backend):
    import json
    ((action, resource, details),) = _restore_audit_row(cfg, edge, backend, old_schema=True)
    assert (action, resource) == ("restore", "database") and "backup_id" in json.loads(details)
