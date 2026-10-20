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
