"""Session touch on authenticated requests: a write transaction of its own, one conditional UPDATE.

Live on MariaDB (snapshot isolation) parallel requests of one session after the touch interval all updated the row
they had read: the losers answered 500 (error 1020). The touch now ends the read transaction first and only moves
last_activity forward from the value the request read; a request that lost the race changes nothing."""
from datetime import datetime, timedelta, timezone

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
