"""Backups: what a restore carries over from the data it replaces (spec section 4, "Carry-over").

SQLite with an attached database stands in for the second schema: the outgoing data is the main database, the
incoming one (the restored backup) is attached as "incoming".
"""
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlmodel import SQLModel

import app.backend.models  # noqa: F401  (every table on SQLModel.metadata)
from app.backend.services.backups.carry_over import (
    CarryOverUnsupported, carry_over, user_state_differences,
)

NOW = datetime(2026, 10, 22, 12, 0, 0, tzinfo=timezone.utc)
NAIVE_NOW = NOW.replace(tzinfo=None)
EARLIER = datetime(2026, 10, 20, 9, 0, 0)


@pytest.fixture
def conn(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'live.db'}")
    with engine.connect() as connection:
        connection.exec_driver_sql(f"ATTACH DATABASE '{tmp_path / 'incoming.db'}' AS incoming")
        SQLModel.metadata.create_all(connection)
        attached = sa.MetaData()
        for table in SQLModel.metadata.sorted_tables:
            table.to_metadata(attached, schema="incoming")
        attached.create_all(connection)
        connection.commit()
        yield connection
    engine.dispose()


def t(name: str, schema=None) -> sa.Table:
    return SQLModel.metadata.tables[name].to_metadata(sa.MetaData(), schema=schema)


def add_user(conn, user_id: int, schema=None, is_active=True, is_deleted=False):
    conn.execute(sa.insert(t("users", schema)).values(
        id=user_id, username=f"u{user_id}", username_alias=f"U{user_id}", is_active=is_active,
        user_type="viewer", is_deleted=is_deleted, created_at=EARLIER, updated_at=EARLIER))


def add_cert(conn, cert_id: int, user_id: int, fingerprint: str, schema=None, **values):
    row = dict(id=cert_id, user_id=user_id, source="issued", label=f"c{cert_id}", serial=f"{cert_id:x}",
               fingerprint_hash=fingerprint, cn_hash="cn", cn_hash_scheme=2, serial_hash=f"s{cert_id}",
               ca_issued=True, export_count=0, created_at=EARLIER, files_dir=f"{user_id}/{cert_id}")
    row.update(values)
    conn.execute(sa.insert(t("user_certificates", schema)).values(**row))


def cert(conn, fingerprint: str, schema="incoming"):
    table = t("user_certificates", schema)
    return conn.execute(sa.select(table).where(table.c.fingerprint_hash == fingerprint)).mappings().first()


def both_users(conn, *ids):
    for user_id in ids:
        add_user(conn, user_id)
        add_user(conn, user_id, "incoming")


def test_revocation_survives(conn):
    both_users(conn, 1)
    add_cert(conn, 10, 1, "fp10", revoked_at=EARLIER, revoked_by=7, revocation_reason="key_compromise")
    add_cert(conn, 10, 1, "fp10", "incoming")
    result = carry_over(conn, None, "incoming", NOW)
    row = cert(conn, "fp10")
    assert (row.revoked_at, row.revoked_by, row.revocation_reason) == (EARLIER, 7, "key_compromise")
    assert result.revocations == 1 and result.certificates_added == 0


def test_new_certificate_inserted_revoked(conn):
    both_users(conn, 1)
    add_cert(conn, 20, 1, "fp20", ca_issued=True, files_dir="1/20")
    add_cert(conn, 21, 1, "fp21", revoked_at=EARLIER, revoked_by=3, revocation_reason="superseded")
    result = carry_over(conn, None, "incoming", NOW)
    new = cert(conn, "fp20")
    assert new.id == 20 and new.revoked_at == NAIVE_NOW and new.revoked_by is None
    assert new.revocation_reason == "cessation_of_operation"
    assert new.ca_issued is True and new.files_dir == "1/20"
    old = cert(conn, "fp21")
    assert (old.revoked_at, old.revoked_by, old.revocation_reason) == (EARLIER, 3, "superseded")
    assert result.certificates_added == 2


def test_inserted_certificate_gets_a_new_id_when_its_id_is_taken(conn):
    both_users(conn, 1)
    add_cert(conn, 30, 1, "fp-out")
    add_cert(conn, 30, 1, "fp-other", "incoming")
    carry_over(conn, None, "incoming", NOW)
    assert cert(conn, "fp-out").id != 30
    assert cert(conn, "fp-other").id == 30


def test_user_of_a_new_certificate_comes_back_archived(conn):
    """A user created after the backup, with a certificate: the user returns archived, the certificate revoked, so
    it goes on the next CRL (spec 8.2)"""
    add_user(conn, 1)
    add_user(conn, 1, "incoming")
    add_user(conn, 2)
    conn.execute(sa.insert(t("role_assignments")).values(user_id=2, role_id=1, scope_key="root", created_at=EARLIER))
    add_cert(conn, 40, 2, "fp40", serial="abc40")
    result = carry_over(conn, None, "incoming", NOW)
    users = t("users", "incoming")
    row = conn.execute(sa.select(users).where(users.c.id == 2)).mappings().one()
    assert (row.username, row.is_active, row.is_deleted) == ("u2", False, True)
    assert result.users_added == ((2, "u2"),)
    new = cert(conn, "fp40")
    assert new.user_id == 2 and new.revoked_at == NAIVE_NOW and new.revocation_reason == "cessation_of_operation"
    assignments = t("role_assignments", "incoming")
    assert conn.execute(sa.select(sa.func.count()).select_from(assignments)).scalar() == 0


def test_placeholder_without_its_post(conn):
    conn.execute(sa.insert(t("posts")).values(id=9, name="P9", code="P9", is_deleted=False))
    add_user(conn, 1, "incoming")
    conn.execute(sa.insert(t("users")).values(
        id=3, username="u3", username_alias="U3", is_active=True, user_type="viewer", is_deleted=False, post_id=9,
        created_at=EARLIER, updated_at=EARLIER))
    add_cert(conn, 41, 3, "fp41")
    carry_over(conn, None, "incoming", NOW)
    users = t("users", "incoming")
    assert conn.execute(sa.select(users.c.post_id).where(users.c.id == 3)).scalar() is None


def test_placeholder_username_clash(conn):
    add_user(conn, 5)
    conn.execute(sa.insert(t("users", "incoming")).values(
        id=6, username="u5", username_alias="other", is_active=True, user_type="viewer", is_deleted=False,
        created_at=EARLIER, updated_at=EARLIER))
    add_cert(conn, 42, 5, "fp42")
    result = carry_over(conn, None, "incoming", NOW)
    assert result.users_added == ((5, "u5~5"),)


def test_users_without_a_new_certificate_are_not_added(conn):
    add_user(conn, 1)
    add_user(conn, 2)
    add_user(conn, 1, "incoming")
    assert carry_over(conn, None, "incoming", NOW).users_added == ()


def set_crl(conn, number, schema=None):
    conn.execute(sa.insert(t("certificate_authority_state", schema)).values(id=1, crl_number=number,
                                                                            revoked_count=0))


@pytest.mark.parametrize("outgoing, incoming, expected", [(12, 9, 12), (3, 9, 9)])
def test_crl_number_is_max(conn, outgoing, incoming, expected):
    set_crl(conn, outgoing)
    set_crl(conn, incoming, "incoming")
    result = carry_over(conn, None, "incoming", NOW)
    table = t("certificate_authority_state", "incoming")
    assert conn.execute(sa.select(table.c.crl_number)).scalar() == expected == result.crl_number


def add_session(conn, session_id: str, ended_at=None, schema="incoming"):
    conn.execute(sa.insert(t("user_sessions", schema)).values(
        id=session_id, user_id=1, last_activity=EARLIER, expires_at=EARLIER + timedelta(hours=1),
        created_at=EARLIER, ended_at=ended_at, end_reason="logout" if ended_at else None))


def test_live_sessions_ended(conn):
    both_users(conn, 1)
    add_session(conn, "a")
    add_session(conn, "b")
    add_session(conn, "c", ended_at=EARLIER)
    result = carry_over(conn, None, "incoming", NOW)
    table = t("user_sessions", "incoming")
    rows = {r.id: r for r in conn.execute(sa.select(table)).mappings()}
    for session_id in ("a", "b"):
        assert rows[session_id].ended_at == NAIVE_NOW
        assert rows[session_id].end_reason == "revoked"
        assert rows[session_id].revoke_cause == "database_restored"
    assert rows["c"].ended_at == EARLIER and rows["c"].end_reason == "logout" and rows["c"].revoke_cause is None
    assert result.sessions_ended == 2


def add_move(conn, move_id: int, status: str, schema=None):
    conn.execute(sa.insert(t("pending_moves", schema)).values(
        id=move_id, node_kind="post", node_id=1, requested_by=1, requested_at=EARLIER, status=status))


def test_waiting_moves_of_the_backup_are_cancelled(conn):
    """A restored database never keeps a move nothing will finish: the uploads it waited for are gone, and a move
    nobody asked for since the backup must not run behind the restore"""
    add_move(conn, 1, "waiting", schema="incoming")
    add_move(conn, 2, "done", schema="incoming")
    result = carry_over(conn, None, "incoming", NOW)
    assert result.moves_cancelled == 1
    table = t("pending_moves", "incoming")
    rows = {row.id: row for row in conn.execute(sa.select(table))}
    assert (rows[1].status, rows[1].finished_at) == ("cancelled", NAIVE_NOW)
    assert (rows[2].status, rows[2].finished_at) == ("done", None)
    assert carry_over(conn, None, "incoming", NOW).moves_cancelled == 0


def test_a_backup_without_the_moves_table(conn):
    conn.exec_driver_sql("DROP TABLE incoming.pending_moves")
    assert carry_over(conn, None, "incoming", NOW).moves_cancelled == 0


def test_idempotent(conn):
    both_users(conn, 1)
    add_cert(conn, 10, 1, "fp10", revoked_at=EARLIER, revocation_reason="key_compromise")
    add_cert(conn, 10, 1, "fp10", "incoming")
    add_cert(conn, 11, 1, "fp11")
    set_crl(conn, 5)
    set_crl(conn, 2, "incoming")
    add_session(conn, "a")
    carry_over(conn, None, "incoming", NOW)
    again = carry_over(conn, None, "incoming", NOW)
    assert (again.revocations, again.certificates_added, again.sessions_ended) == (0, 0, 0)
    assert again.crl_number == 5


def test_user_state_differences(conn):
    add_user(conn, 1)
    add_user(conn, 1, "incoming", is_active=False)
    add_user(conn, 2, is_deleted=True)
    add_user(conn, 2, "incoming")
    add_user(conn, 3)
    add_user(conn, 3, "incoming")
    assert user_state_differences(conn, None, "incoming") == [
        (1, "u1", "is_active True->False"), (2, "u2", "is_deleted True->False")]


def test_crl_state_row_follows_the_newer_number(conn):
    """The dates of the newer CRL come with its number (seen live: /health reported a stale CRL after an undo)"""
    published = datetime(2026, 10, 22, 11, 0, 0)
    conn.execute(sa.insert(t("certificate_authority_state")).values(
        id=1, crl_number=12, this_update=published, next_update=published + timedelta(days=7), published_at=published,
        revoked_count=5))
    conn.execute(sa.insert(t("certificate_authority_state", "incoming")).values(
        id=1, crl_number=9, this_update=EARLIER, next_update=EARLIER + timedelta(days=7), published_at=EARLIER,
        revoked_count=2))
    carry_over(conn, None, "incoming", NOW)
    row = conn.execute(sa.select(t("certificate_authority_state", "incoming"))).mappings().one()
    assert (row.crl_number, row.this_update, row.next_update, row.published_at, row.revoked_count) == (
        12, published, published + timedelta(days=7), published, 5)


def _older_incoming(conn):
    """Tables of the incoming schema as an older migration left them: user_certificates without the export
    columns, user_sessions with is_active instead of ended_at / end_reason / revoke_cause"""
    conn.exec_driver_sql("DROP TABLE incoming.user_certificates")
    conn.exec_driver_sql(
        "CREATE TABLE incoming.user_certificates (id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, source VARCHAR(16), "
        "label VARCHAR(64), serial VARCHAR(64), fingerprint_hash VARCHAR(64) NOT NULL, cn_hash VARCHAR(64), "
        "cn_hash_scheme SMALLINT, serial_hash VARCHAR(64), ca_issued BOOLEAN, revoked_at DATETIME, revoked_by INTEGER, "
        "revocation_reason VARCHAR(32), files_dir VARCHAR(64), created_at DATETIME)")
    conn.exec_driver_sql("DROP TABLE incoming.user_sessions")
    conn.exec_driver_sql(
        "CREATE TABLE incoming.user_sessions (id VARCHAR(128) PRIMARY KEY, user_id INTEGER NOT NULL, "
        "last_activity DATETIME, expires_at DATETIME, is_active BOOLEAN, created_at DATETIME)")


def test_restore_into_an_older_schema(conn):
    """A backup from before a migration that changed these tables (review: Unknown column on MariaDB)"""
    _older_incoming(conn)
    both_users(conn, 1)
    add_cert(conn, 10, 1, "fp10", revoked_at=EARLIER, revocation_reason="key_compromise")
    conn.exec_driver_sql("INSERT INTO incoming.user_certificates (id, user_id, fingerprint_hash) VALUES (10, 1, 'fp10')")
    add_cert(conn, 11, 1, "fp11", export_count=3)
    conn.exec_driver_sql("INSERT INTO incoming.user_sessions (id, user_id, is_active) VALUES ('a', 1, 1), ('b', 1, 0)")
    result = carry_over(conn, None, "incoming", NOW)
    assert (result.revocations, result.certificates_added, result.sessions_ended) == (1, 1, 1)
    rows = conn.exec_driver_sql(
        "SELECT fingerprint_hash, revocation_reason FROM incoming.user_certificates ORDER BY id").all()
    assert rows == [("fp10", "key_compromise"), ("fp11", "cessation_of_operation")]
    assert conn.exec_driver_sql("SELECT id FROM incoming.user_sessions WHERE is_active").all() == []


def test_backup_without_a_certificates_table_is_refused(conn):
    conn.exec_driver_sql("DROP TABLE incoming.user_certificates")
    both_users(conn, 1)
    add_cert(conn, 10, 1, "fp10", revoked_at=EARLIER, revocation_reason="key_compromise")
    with pytest.raises(CarryOverUnsupported):
        carry_over(conn, None, "incoming", NOW)


def test_placeholder_username_clash_ignores_case(conn):
    """MariaDB's unique index compares usernames without case: Ivan and ivan clash (review)"""
    add_user(conn, 5)
    conn.execute(sa.insert(t("users", "incoming")).values(
        id=6, username="U5", username_alias="other", is_active=True, user_type="viewer", is_deleted=False,
        created_at=EARLIER, updated_at=EARLIER))
    add_cert(conn, 43, 5, "fp43")
    assert carry_over(conn, None, "incoming", NOW).users_added == ((5, "u5~5"),)


def test_placeholder_username_stays_within_the_column(conn):
    long_name = "x" * 255
    conn.execute(sa.insert(t("users")).values(
        id=7, username=long_name, username_alias="L", is_active=True, user_type="viewer", is_deleted=False,
        created_at=EARLIER, updated_at=EARLIER))
    conn.execute(sa.insert(t("users", "incoming")).values(
        id=8, username=long_name, username_alias="other", is_active=True, user_type="viewer", is_deleted=False,
        created_at=EARLIER, updated_at=EARLIER))
    add_cert(conn, 44, 7, "fp44")
    ((user_id, username),) = carry_over(conn, None, "incoming", NOW).users_added
    assert user_id == 7 and len(username) <= 255 and username.endswith("~7")


def add_event(conn, event_id: int, schema=None):
    conn.execute(sa.insert(t("outbox_events", schema)).values(
        id=event_id, type="scope.changed", v=1, scope_key="root", permission="post.view", payload="{}",
        occurred_at=EARLIER))


def test_events_of_the_backup_are_removed_and_the_live_ones_not_carried(conn):
    """Delivery data: the restored rows must not replay, the outgoing ones describe data the restore replaced"""
    add_event(conn, 1, schema="incoming")
    add_event(conn, 2, schema="incoming")
    add_event(conn, 7)
    result = carry_over(conn, None, "incoming", NOW)
    assert result.events_removed == 2
    assert conn.execute(sa.select(t("outbox_events", "incoming"))).all() == []
    assert [row.id for row in conn.execute(sa.select(t("outbox_events")))] == [7], "the outgoing table is not touched"
    assert carry_over(conn, None, "incoming", NOW).events_removed == 0


def test_a_backup_without_the_events_table(conn):
    conn.exec_driver_sql("DROP TABLE incoming.outbox_events")
    assert carry_over(conn, None, "incoming", NOW).events_removed == 0


def test_the_events_table_keeps_its_counter_for_the_raise():
    """DELETE, not TRUNCATE: raise_statements raises AUTO_INCREMENT past the replaced data for every table both schemas
    have, outbox_events included"""
    from app.backend.services.backups.schemas import raise_statements
    statements = raise_statements("db_restore", {"outbox_events": 500}, {"outbox_events": 3})
    assert any("outbox_events" in statement and "500" in statement for statement in statements)
