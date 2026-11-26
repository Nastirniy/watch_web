"""A stream and a background request do not count as activity (X-Activity: background): every check of a request
runs, only the touch of the session is left out. A refresh with the header still rotates its tokens and keeps the
idle end where it was."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import update
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.auth.service import AuthService
from app.backend.auth.sessions import utc
from app.backend.enums.general import ActivityHint
from app.backend.enums.routes import AuthRoutes
from app.backend.models.user import UserSession
from tests.helpers import make_user

ME = V1.path(AuthRoutes.ME)
REFRESH = V1.path(AuthRoutes.REFRESH)
BACKGROUND = {"X-Activity": ActivityHint.BACKGROUND.value}


async def _session_of(session_maker, token_headers) -> UserSession:
    claims = AuthService.verify_token(token_headers["Authorization"].split(" ", 1)[1], "access")
    async with session_maker() as fresh:
        return (await fresh.exec(select(UserSession).where(UserSession.id == claims["sid"]))).one()


async def _age(db, session_id: str, minutes: int = 5) -> datetime:
    """The session's last activity lies this far back, older than the touch interval, its end 30 minutes after it"""
    then = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    await db.execute(update(UserSession).where(UserSession.id == session_id)
                     .values(last_activity=then, expires_at=then + timedelta(minutes=30)))
    await db.commit()
    return then


async def test_a_background_request_does_not_touch_the_session(client, db, headers, session_maker):
    user = await make_user(db)
    token = headers(user)
    assert (await client.get(ME, headers=token)).status_code == 200      # stores the session
    row = await _session_of(session_maker, token)
    then = await _age(db, row.id)
    answer = await client.get(ME, headers={**token, **BACKGROUND})
    assert answer.status_code == 200, answer.text
    stored = await _session_of(session_maker, token)
    assert abs(utc(stored.last_activity) - then) < timedelta(seconds=1)
    assert abs(utc(stored.expires_at) - (then + timedelta(minutes=30))) < timedelta(seconds=1)


async def test_a_normal_request_still_touches(client, db, headers, session_maker):
    user = await make_user(db)
    token = headers(user)
    assert (await client.get(ME, headers=token)).status_code == 200
    then = await _age(db, (await _session_of(session_maker, token)).id)
    for value in (None, "foreground", "BACKGROUND"):        # anything else is activity
        await _age(db, (await _session_of(session_maker, token)).id)
        extra = {} if value is None else {"X-Activity": value}
        assert (await client.get(ME, headers={**token, **extra})).status_code == 200
        assert utc((await _session_of(session_maker, token)).last_activity) > then + timedelta(minutes=4), value


async def test_a_background_refresh_rotates_the_token_and_keeps_the_idle_end(client, db, session_maker):
    user = await make_user(db)
    row = await AuthService.create_session(db, user)
    refresh_token = AuthService.session_tokens(user.id, row)[1]
    then = await _age(db, row.id)
    answer = await client.post(REFRESH, json={"refresh_token": refresh_token}, headers=BACKGROUND)
    assert answer.status_code == 200, answer.text
    assert answer.json()["refresh_token"] != refresh_token
    async with session_maker() as fresh:
        stored = (await fresh.exec(select(UserSession).where(UserSession.id == row.id))).one()
    assert abs(utc(stored.last_activity) - then) < timedelta(seconds=1)
    assert abs(utc(stored.expires_at) - (then + timedelta(minutes=30))) < timedelta(seconds=1)
    # the old token is used now: the rotation happened
    again = await client.post(REFRESH, json={"refresh_token": refresh_token}, headers=BACKGROUND)
    assert again.status_code == 401


async def test_a_normal_refresh_still_touches(client, db, session_maker):
    user = await make_user(db)
    row = await AuthService.create_session(db, user)
    refresh_token = AuthService.session_tokens(user.id, row)[1]
    then = await _age(db, row.id)
    assert (await client.post(REFRESH, json={"refresh_token": refresh_token})).status_code == 200
    async with session_maker() as fresh:
        stored = (await fresh.exec(select(UserSession).where(UserSession.id == row.id))).one()
    assert utc(stored.last_activity) > then + timedelta(minutes=4)


async def test_a_background_request_is_authorized_like_any_other(client, db, headers, session_maker):
    user = await make_user(db)
    token = headers(user)
    assert (await client.get(ME, headers=token)).status_code == 200
    row = await _session_of(session_maker, token)
    # an idle session past its end: session_expired with the header too
    then = datetime.now(timezone.utc) - timedelta(hours=2)
    await db.execute(update(UserSession).where(UserSession.id == row.id)
                     .values(last_activity=then, expires_at=then + timedelta(minutes=30)))
    await db.commit()
    answer = await client.get(ME, headers={**token, **BACKGROUND})
    assert answer.status_code == 401 and answer.json()["code"] == "session_expired", answer.text
    assert (await client.get(ME, headers=BACKGROUND)).status_code == 401
