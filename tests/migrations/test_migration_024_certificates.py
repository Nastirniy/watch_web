"""024_user_certificates: the certificate hashes of users become legacy rows of user_certificates, the columns leave
users; the downgrade gives users their columns back (exactly from a legacy row that is not revoked, else the newest
active row, else a placeholder that matches no certificate) and drops both tables.

The legacy data is written into a database downgraded to 023_authorization_v2, the shape production has before the
upgrade (as the tests of 023 do at 022)."""
import sqlite3
from pathlib import Path
from typing import Dict, List

import pytest

from tests.migrations.test_column_bounds import _run_downgrade
from tests.migrations.test_migrations import HEAD, run_migrate

PREVIOUS = "023_authorization_v2"
STAMP = "2026-01-01 00:00:00"
USER_COLUMNS = ("pki_certificate_cn_hash", "pki_certificate_cn_hash_scheme", "pki_certificate_serial_hash",
                "pki_certificate_fingerprint_hash")
NEW_TABLES = {"user_certificates", "certificate_authority_state"}
USERS = {   # id: (username, cn hash, scheme, serial hash, fingerprint hash, created_at)
    1: ("old_scheme", "cn-old", 1, "sn-old", "fp-old", "2025-03-04 05:06:07"),
    2: ("new_scheme", "cn-new", 2, "sn-new", "fp-new", "2026-01-02 03:04:05"),
}


def tables(conn) -> set:
    return {row[0] for row in conn.execute("select name from sqlite_master where type='table'")}


def user_columns(conn) -> Dict[str, tuple]:
    return {row[1]: row for row in conn.execute("pragma table_info(users)")}


def unique_columns(conn, table: str) -> set:
    """Column sets of the unique indexes of a table"""
    return {frozenset(c[2] for c in conn.execute(f"pragma index_info('{row[1]}')"))
            for row in conn.execute(f"pragma index_list({table})") if row[2] == 1}


def migrated(tmp_path: Path, name: str) -> Path:
    db_file = tmp_path / name
    first = run_migrate(db_file)
    assert first.returncode == 0, first.stdout + first.stderr
    return db_file


def database_at_023(tmp_path: Path) -> Path:
    """A database at 023 whose users hold the certificate hashes (a fresh install, downgraded, then filled)"""
    db_file = migrated(tmp_path, "legacy.db")
    down = _run_downgrade(db_file, PREVIOUS)
    assert down.returncode == 0, down.stdout + down.stderr
    conn = sqlite3.connect(db_file)
    assert conn.execute("select version_num from alembic_version").fetchone()[0] == PREVIOUS
    for user_id, (username, cn, scheme, serial, fingerprint, created) in USERS.items():
        conn.execute(
            "insert into users (id, pki_certificate_cn_hash, pki_certificate_cn_hash_scheme, pki_certificate_serial_hash,"
            " pki_certificate_fingerprint_hash, username, username_alias, is_active, is_deleted, created_at,"
            " updated_at, user_type) values (?, ?, ?, ?, ?, ?, ?, 1, 0, ?, ?, 'viewer')",
            (user_id, cn, scheme, serial, fingerprint, username, username.title(), created, STAMP))
    conn.commit()
    conn.close()
    return db_file


def snapshot(db_file: Path) -> List[dict]:
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    rows = [dict(row) for row in conn.execute("select * from users order by id")]
    conn.close()
    return rows


def insert_user(conn, user_id: int, username: str) -> None:
    conn.execute("insert into users (id, username, username_alias, is_active, is_deleted, created_at, updated_at,"
                 " user_type) values (?, ?, ?, 1, 0, ?, ?, 'viewer')", (user_id, username, username.title(), STAMP, STAMP))


def insert_certificate(conn, user_id: int, name: str, source: str = "uploaded", created_at: str = STAMP,
                       cn_hash: str = None, serial_hash: str = None, not_after: str = None, revoked_at: str = None,
                       files_dir: str = None, export_count: int = 0, scheme: int = 2) -> None:
    conn.execute(
        "insert into user_certificates (user_id, source, label, subject_cn, serial, not_before, not_after,"
        " fingerprint_hash, cn_hash, cn_hash_scheme, serial_hash, ca_issued, revoked_at, revocation_reason, files_dir,"
        " export_count, created_at) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, source, name, name, f"{user_id:x}", STAMP, not_after, f"fp-{name}", cn_hash or f"cn-{name}", scheme,
         serial_hash or f"sn-{name}", 1 if source == "issued" else 0, revoked_at,
         "superseded" if revoked_at else None, files_dir, export_count, created_at))


def downgraded_users(db_file: Path) -> tuple:
    """Downgrade to 023; (the restored hashes of every user, the output)"""
    down = _run_downgrade(db_file, PREVIOUS)
    assert down.returncode == 0, down.stdout + down.stderr
    conn = sqlite3.connect(db_file)
    assert conn.execute("select version_num from alembic_version").fetchone()[0] == PREVIOUS
    assert not NEW_TABLES & tables(conn)
    restored = {row[0]: tuple(row[1:]) for row in conn.execute(
        "select id, pki_certificate_cn_hash, pki_certificate_cn_hash_scheme, pki_certificate_serial_hash,"
        " pki_certificate_fingerprint_hash from users order by id")}
    conn.close()
    return restored, down.stdout + down.stderr


@pytest.mark.slow
def test_upgrade_turns_user_hashes_into_legacy_rows(tmp_path):
    db_file = database_at_023(tmp_path)
    result = run_migrate(db_file)
    assert result.returncode == 0, result.stdout + result.stderr

    conn = sqlite3.connect(db_file)
    assert conn.execute("select version_num from alembic_version").fetchone()[0] == HEAD
    rows = conn.execute(
        "select user_id, source, label, subject_cn, serial, not_before, not_after, fingerprint_hash, cn_hash,"
        " cn_hash_scheme, serial_hash, ca_issued, revoked_at, revoked_by, revocation_reason, files_dir, export_count,"
        " last_exported_at, created_at, created_by from user_certificates order by user_id").fetchall()
    assert rows == [
        (user_id, "legacy", "", None, None, None, None, fingerprint, cn, scheme, serial, 0, None, None, None, None, 0,
         None, created, None)
        for user_id, (_, cn, scheme, serial, fingerprint, created) in USERS.items()
    ]
    assert not set(USER_COLUMNS) & set(user_columns(conn))
    assert not any("pki_certificate" in row[1] for row in conn.execute("pragma index_list(users)"))
    assert conn.execute("select id, crl_number, revoked_count, this_update, next_update, published_at"
                        " from certificate_authority_state").fetchall() == [(1, 0, 0, None, None, None)]
    assert frozenset({"fingerprint_hash"}) in unique_columns(conn, "user_certificates")
    indexes = {row[1] for row in conn.execute("pragma index_list(user_certificates)")}
    assert {"ix_user_certificates_user_id", "ix_user_certificates_crl"} <= indexes
    conn.close()

    # 024 again on the migrated database (stamped back): nothing copied twice, no second state row
    conn = sqlite3.connect(db_file)
    conn.execute("update alembic_version set version_num = ?", (PREVIOUS,))
    conn.commit()
    conn.close()
    again = run_migrate(db_file)
    assert again.returncode == 0, again.stdout + again.stderr
    conn = sqlite3.connect(db_file)
    assert conn.execute("select count(*) from user_certificates").fetchone()[0] == len(USERS)
    assert conn.execute("select count(*) from certificate_authority_state").fetchone()[0] == 1
    conn.close()


@pytest.mark.slow
def test_downgrade_restores_exactly(tmp_path):
    db_file = database_at_023(tmp_path)
    before = snapshot(db_file)
    assert run_migrate(db_file).returncode == 0

    restored, _ = downgraded_users(db_file)
    assert snapshot(db_file) == before, "every user gets the values of its legacy row back"
    conn = sqlite3.connect(db_file)
    columns = user_columns(conn)
    _, _, column_type, not_null, default, _ = columns["pki_certificate_cn_hash_scheme"]
    assert (column_type, not_null, default) == ("SMALLINT", 1, "'1'"), "as 019 adds it"
    assert all(columns[name][3] == 1 for name in USER_COLUMNS), "NOT NULL"
    unique = unique_columns(conn, "users")
    assert {frozenset({"pki_certificate_cn_hash"}), frozenset({"pki_certificate_serial_hash"}),
            frozenset({"pki_certificate_fingerprint_hash"})} <= unique
    conn.close()

    # and up again: the same legacy rows
    again = run_migrate(db_file)
    assert again.returncode == 0, again.stdout + again.stderr
    conn = sqlite3.connect(db_file)
    assert conn.execute("select user_id, fingerprint_hash, cn_hash_scheme from user_certificates order by user_id"
                        ).fetchall() == [(1, "fp-old", 1), (2, "fp-new", 2)]
    conn.close()


@pytest.mark.slow
def test_downgrade_takes_the_newest_active_row(tmp_path):
    db_file = migrated(tmp_path, "newest.db")
    conn = sqlite3.connect(db_file)
    insert_user(conn, 1, "renewing")
    insert_certificate(conn, 1, "older", created_at="2026-01-01 00:00:00")
    insert_certificate(conn, 1, "newer", created_at="2026-02-01 00:00:00", not_after="2099-01-01 00:00:00")
    insert_certificate(conn, 1, "revoked", created_at="2026-03-01 00:00:00", revoked_at="2026-03-02 00:00:00")
    insert_certificate(conn, 1, "expired", created_at="2026-04-01 00:00:00", not_after="2026-04-02 00:00:00")
    insert_certificate(conn, 1, "issued", source="issued", created_at="2026-05-01 00:00:00",
                       revoked_at="2026-05-02 00:00:00", files_dir="1/5", export_count=2)
    conn.commit()
    conn.close()

    restored, output = downgraded_users(db_file)
    assert restored == {1: ("cn-newer", 2, "sn-newer", "fp-newer")}
    assert "5 certificate row(s)" in output and "1 of them issued" in output and "2 revoked" in output, output
    assert "user 1 (" not in output, "a user with an active row gets no placeholder"


@pytest.mark.slow
def test_downgrade_placeholder_for_a_taken_cn_hash(tmp_path):
    """The columns are unique: a user whose CN or serial hash another user's restored row holds gets a placeholder,
    like a user without an active certificate. Legacy rows come back first, then the other users by id."""
    db_file = migrated(tmp_path, "placeholder.db")
    conn = sqlite3.connect(db_file)
    for user_id, username in enumerate(("first", "second", "nothing", "serial", "early", "legacy"), start=1):
        insert_user(conn, user_id, username)
    insert_certificate(conn, 1, "laptop-1", cn_hash="cn-laptop", serial_hash="sn-1")
    insert_certificate(conn, 2, "laptop-2", cn_hash="cn-laptop", serial_hash="sn-2")
    insert_certificate(conn, 4, "serial-4", serial_hash="sn-1")
    insert_certificate(conn, 5, "early-5", cn_hash="cn-kept")
    insert_certificate(conn, 6, "legacy-6", source="legacy", cn_hash="cn-kept", scheme=1)
    conn.commit()
    conn.close()

    restored, output = downgraded_users(db_file)
    placeholder = {user_id: (f"unassigned:{user_id}", 1, f"unassigned:{user_id}", f"unassigned:{user_id}")
                   for user_id in (2, 3, 4, 5)}
    assert restored == {
        1: ("cn-laptop", 2, "sn-1", "fp-laptop-1"),
        **placeholder,
        6: ("cn-kept", 1, "sn-legacy-6", "fp-legacy-6"),
    }
    for user_id, username in ((2, "second"), (3, "nothing"), (4, "serial"), (5, "early")):
        assert f"user {user_id} ({username})" in output, output
    assert "user 1 (" not in output and "user 6 (" not in output


@pytest.mark.slow
def test_downgrade_never_revives_a_revoked_legacy_certificate(tmp_path):
    """The older code knows no revocation: a revoked legacy row is no candidate. The user gets their newest active
    row, else the placeholder (fails closed); the output names them."""
    db_file = migrated(tmp_path, "revoked.db")
    conn = sqlite3.connect(db_file)
    insert_user(conn, 1, "renewed")
    insert_user(conn, 2, "revoked")
    insert_certificate(conn, 1, "legacy-1", source="legacy", scheme=1, revoked_at="2026-03-02 00:00:00")
    insert_certificate(conn, 1, "uploaded-1", created_at="2026-04-01 00:00:00")
    insert_certificate(conn, 2, "legacy-2", source="legacy", scheme=1, revoked_at="2026-03-02 00:00:00")
    conn.commit()
    conn.close()

    restored, output = downgraded_users(db_file)
    assert restored == {
        1: ("cn-uploaded-1", 2, "sn-uploaded-1", "fp-uploaded-1"),
        2: ("unassigned:2", 1, "unassigned:2", "unassigned:2"),
    }
    assert "user 1 (renewed): its legacy certificate is revoked" in output, output
    assert "user 2 (revoked): its legacy certificate is revoked" in output, output
    assert "user 2 (revoked): no active certificate; placeholder unassigned:2" in output, output
    assert "logs in again" not in output, output
