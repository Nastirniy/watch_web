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
    OrphanCertificates, carry_over, user_state_differences,
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
    result = carry_over(conn, None, "incoming", NOW, skip_orphans=False)
    row = cert(conn, "fp10")
    assert (row.revoked_at, row.revoked_by, row.revocation_reason) == (EARLIER, 7, "key_compromise")
    assert result.revocations == 1 and result.certificates_added == 0


def test_new_certificate_inserted_revoked(conn):
    both_users(conn, 1)
    add_cert(conn, 20, 1, "fp20", ca_issued=True, files_dir="1/20")
    add_cert(conn, 21, 1, "fp21", revoked_at=EARLIER, revoked_by=3, revocation_reason="superseded")
    result = carry_over(conn, None, "incoming", NOW, skip_orphans=False)
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
    carry_over(conn, None, "incoming", NOW, skip_orphans=False)
    assert cert(conn, "fp-out").id != 30
    assert cert(conn, "fp-other").id == 30


def test_orphan_refused_then_skipped(conn):
    add_user(conn, 1)
    add_user(conn, 2)
    add_user(conn, 1, "incoming")
    add_cert(conn, 40, 2, "fp40", serial="abc40")
    add_cert(conn, 41, 1, "fp41")
    with pytest.raises(OrphanCertificates) as exc:
        carry_over(conn, None, "incoming", NOW, skip_orphans=False)
    assert exc.value.serials == ("abc40",)
    assert cert(conn, "fp41") is None and cert(conn, "fp40") is None
    result = carry_over(conn, None, "incoming", NOW, skip_orphans=True)
    assert result.orphan_serials == ("abc40",)
    assert cert(conn, "fp40") is None and cert(conn, "fp41") is not None


def set_crl(conn, number, schema=None):
    conn.execute(sa.insert(t("certificate_authority_state", schema)).values(id=1, crl_number=number,
                                                                            revoked_count=0))


@pytest.mark.parametrize("outgoing, incoming, expected", [(12, 9, 12), (3, 9, 9)])
def test_crl_number_is_max(conn, outgoing, incoming, expected):
    set_crl(conn, outgoing)
    set_crl(conn, incoming, "incoming")
    result = carry_over(conn, None, "incoming", NOW, skip_orphans=False)
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
    result = carry_over(conn, None, "incoming", NOW, skip_orphans=False)
    table = t("user_sessions", "incoming")
    rows = {r.id: r for r in conn.execute(sa.select(table)).mappings()}
    for session_id in ("a", "b"):
        assert rows[session_id].ended_at == NAIVE_NOW
        assert rows[session_id].end_reason == "revoked"
        assert rows[session_id].revoke_cause == "database_restored"
    assert rows["c"].ended_at == EARLIER and rows["c"].end_reason == "logout" and rows["c"].revoke_cause is None
    assert result.sessions_ended == 2


def test_idempotent(conn):
    both_users(conn, 1)
    add_cert(conn, 10, 1, "fp10", revoked_at=EARLIER, revocation_reason="key_compromise")
    add_cert(conn, 10, 1, "fp10", "incoming")
    add_cert(conn, 11, 1, "fp11")
    set_crl(conn, 5)
    set_crl(conn, 2, "incoming")
    add_session(conn, "a")
    carry_over(conn, None, "incoming", NOW, skip_orphans=False)
    again = carry_over(conn, None, "incoming", NOW, skip_orphans=False)
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
    carry_over(conn, None, "incoming", NOW, skip_orphans=False)
    row = conn.execute(sa.select(t("certificate_authority_state", "incoming"))).mappings().one()
    assert (row.crl_number, row.this_update, row.next_update, row.published_at, row.revoked_count) == (
        12, published, published + timedelta(days=7), published, 5)
