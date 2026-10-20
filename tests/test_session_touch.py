"""Session touch on authenticated requests: a write transaction of its own, one conditional UPDATE.

Live on MariaDB (snapshot isolation) parallel requests of one session after the touch interval all updated the row
they had read: the losers answered 500 (error 1020). The touch now ends the read transaction first and only moves
last_activity forward from the value the request read; a request that lost the race changes nothing."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import event
from sqlmodel import select

from app.backend.auth.sessions import touch_session, utc
from app.backend.models.user import UserSession
from tests.helpers import make_user


async def _row(db, user, last_activity):
    row = UserSession(id=f"touch-{user.id}", user_id=user.id, created_at=last_activity, last_activity=last_activity,
                      expires_at=last_activity + timedelta(minutes=30))
    db.add(row)
    await db.commit()
    return row


async def _stored(session_maker, session_id):
    async with session_maker() as fresh:
        return (await fresh.exec(select(UserSession).where(UserSession.id == session_id))).one()


async def test_touch_moves_activity_and_end(db, session_maker):
    user = await make_user(db)
    then = datetime.now(timezone.utc) - timedelta(minutes=5)
    row = await _row(db, user, then)
    async with session_maker() as session:
        read = (await session.exec(select(UserSession).where(UserSession.id == row.id))).one()
        assert await touch_session(session, read) is True
    stored = await _stored(session_maker, row.id)
    assert utc(stored.last_activity) > then + timedelta(minutes=4)
    assert utc(stored.expires_at) > utc(stored.last_activity)


async def test_a_request_that_read_an_older_activity_changes_nothing(db, session_maker):
    user = await make_user(db)
    then = datetime.now(timezone.utc) - timedelta(minutes=5)
    row = await _row(db, user, then)
    async with session_maker() as slow, session_maker() as fast:
        stale = (await slow.exec(select(UserSession).where(UserSession.id == row.id))).one()
        current = (await fast.exec(select(UserSession).where(UserSession.id == row.id))).one()
        assert await touch_session(fast, current) is True
        moved = (await _stored(session_maker, row.id)).last_activity
        assert await touch_session(slow, stale) is False
    assert (await _stored(session_maker, row.id)).last_activity == moved


async def test_an_end_recorded_by_another_request_is_kept(db, session_maker):
    """Parallel requests of an expired session each recorded its end: the later ones wrote over the row the first
    one changed (a 500 on MariaDB, 1020) and could overwrite an end recorded in between (a revocation)"""
    from app.backend.auth.sessions import record_end, session_state
    from app.backend.enums.general import SessionEndReason, SessionRevokeCause
    user = await make_user(db)
    then = datetime.now(timezone.utc) - timedelta(days=2)
    row = await _row(db, user, then)
    async with session_maker() as stale_session:
        stale = (await stale_session.exec(select(UserSession).where(UserSession.id == row.id))).one()
        async with session_maker() as admin:
            ended = (await admin.exec(select(UserSession).where(UserSession.id == row.id))).one()
            ended.ended_at, ended.end_reason = datetime.now(timezone.utc), SessionEndReason.REVOKED.value
            ended.revoke_cause = SessionRevokeCause.ENDED_BY_ADMIN.value
            admin.add(ended)
            await admin.commit()
        await record_end(stale_session, stale, session_state(stale))
    stored = await _stored(session_maker, row.id)
    assert stored.end_reason == SessionEndReason.REVOKED.value


async def test_touch_never_moves_an_ended_session_and_updates_the_row_in_memory(db, session_maker):
    user = await make_user(db)
    then = datetime.now(timezone.utc) - timedelta(minutes=5)
    row = await _row(db, user, then)
    async with session_maker() as session:
        read = (await session.exec(select(UserSession).where(UserSession.id == row.id))).one()
        assert await touch_session(session, read) is True
        assert utc(read.last_activity) > then + timedelta(minutes=4)     # the caller's copy shows the touch
    async with session_maker() as session:
        read = (await session.exec(select(UserSession).where(UserSession.id == row.id))).one()
        async with session_maker() as other:
            ended = (await other.exec(select(UserSession).where(UserSession.id == row.id))).one()
            ended.ended_at, ended.end_reason = datetime.now(timezone.utc), "logout"
            other.add(ended)
            await other.commit()
        assert await touch_session(session, read) is False     # read before the logout: never moves an ended row


async def test_ending_a_session_of_an_inactive_certificate_twice_keeps_the_first_end(db, session_maker):
    """Every parallel request of a session whose certificate expired ended the session: the later ones wrote over
    the row the first one changed (1020 on MariaDB) and logged a second force_logout"""
    from app.backend.auth.service import AuthService
    from app.backend.models.audit import AuditLog
    user = await make_user(db)
    row = await _row(db, user, datetime.now(timezone.utc) - timedelta(minutes=1))
    row.certificate_fingerprint_hash = "no-such-certificate"
    db.add(row)
    await db.commit()
    async with session_maker() as first, session_maker() as second:
        a = (await first.exec(select(UserSession).where(UserSession.id == row.id))).one()
        b = (await second.exec(select(UserSession).where(UserSession.id == row.id))).one()
        assert await AuthService.end_session_of_revoked_certificate(first, a, user) is True
        assert await AuthService.end_session_of_revoked_certificate(second, b, user) is True
    stored = await _stored(session_maker, row.id)
    assert stored.end_reason == "revoked" and stored.revoke_cause == "certificate_revoked"
    async with session_maker() as fresh:
        logged = (await fresh.exec(select(AuditLog).where(AuditLog.action == "force_logout",
                                                          AuditLog.session_id == row.id))).all()
    assert len(logged) == 1


def _statements(engine):
    """The statements executed while the returned list is open, whitespace folded, with "COMMIT" where a
    transaction ended (sqlite drops FOR UPDATE / FOR SHARE: the order is what the tests can see)"""
    seen = []

    def execute(conn, cursor, statement, parameters, context, executemany):
        seen.append(" ".join(statement.split()))

    def commit(conn):
        seen.append("COMMIT")

    event.listen(engine.sync_engine, "before_cursor_execute", execute)
    event.listen(engine.sync_engine, "commit", commit)

    def stop():
        event.remove(engine.sync_engine, "before_cursor_execute", execute)
        event.remove(engine.sync_engine, "commit", commit)
    return seen, stop


def _write_of(seen, marker):
    """The statements of the transaction that ran the first statement starting with marker"""
    at = next(i for i, statement in enumerate(seen) if statement.startswith(marker))
    begin = max((i for i in range(at) if seen[i] == "COMMIT"), default=-1) + 1
    end = next((i for i in range(at, len(seen)) if seen[i] == "COMMIT"), len(seen))
    return seen[begin:end]


async def test_touch_takes_a_shared_lock_on_its_user_first(db, session_maker, engine):
    """Live on MariaDB an administrator ending the sessions of a user (or revoking a certificate) read the sessions
    and then updated them; a touch committed in between made that update answer 500 (error 1020). Every writer of a
    user's sessions now takes the user row first: the administrator FOR UPDATE, the touch shared."""
    user = await make_user(db)
    row = await _row(db, user, datetime.now(timezone.utc) - timedelta(minutes=5))
    async with session_maker() as session:
        read = (await session.exec(select(UserSession).where(UserSession.id == row.id))).one()
        seen, stop = _statements(engine)
        try:
            assert await touch_session(session, read) is True
        finally:
            stop()
    write = _write_of(seen, "UPDATE user_sessions")
    assert write[0].startswith("SELECT users.id FROM users WHERE users.id = ?"), write
    assert write[1].startswith("UPDATE user_sessions"), write


async def test_certificate_revoke_locks_the_user_first(client, db, headers, engine):
    """The revocation ends the sessions of the certificate: like every other end of a user's sessions it begins with
    the user row, so it waits for a touch of the user instead of meeting it (1020)"""
    from app.backend.api.v1 import V1
    from app.backend.enums.general import CertificateSource
    from app.backend.enums.routes import UserRoutes
    from tests.helpers import make_admin, make_certificate_row
    admin = await make_admin(db)
    user = await make_user(db)
    certificate = await make_certificate_row(db, user, CertificateSource.LEGACY)
    seen, stop = _statements(engine)
    try:
        response = await client.post(
            V1.path(UserRoutes.USER_CERTIFICATE_REVOKE, user_id=user.id, certificate_id=certificate.id),
            headers=headers(admin))
    finally:
        stop()
    assert response.status_code == 200, response.text
    write = _write_of(seen, "UPDATE user_certificates")
    assert write[0].startswith("SELECT users.id FROM users WHERE users.id = ?"), write
    assert write[1].startswith("SELECT user_certificates."), write
