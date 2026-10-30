"""Refresh token reuse (RFC 9700 section 4.14): a used refresh token presented again ends its session, except the
immediately previous one within security.refresh_reuse_grace_seconds of its rotation (two tabs refreshing at the
same moment: the other one holds the new token).

The session stores the jti of the previous token and when it was rotated (migration 034, auth/sessions.py
claim_refresh); a session without them (from before 034) answers a reuse as before: invalid_refresh_token, the
session stays."""
from datetime import timedelta

import pytest
from jose import jwt
from sqlalchemy import update
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.auth import sessions as session_lifetimes
from app.backend.auth.service import AuthService
from app.backend.config import settings
from app.backend.enums.general import SessionEndReason, SessionRevokeCause
from app.backend.enums.routes import AuthRoutes
from app.backend.models.audit import AuditLog
from app.backend.models.user import UserSession
from tests.helpers import make_user

REFRESH = V1.path(AuthRoutes.REFRESH)
ME = V1.path(AuthRoutes.ME)


async def _stored(session_maker, session_id) -> UserSession:
    async with session_maker() as fresh:
        return (await fresh.exec(select(UserSession).where(UserSession.id == session_id))).one()


async def _rotated_ago(session_maker, session_id, ago: timedelta) -> None:
    """Move the last rotation of the session back in time"""
    async with session_maker() as other:
        row = (await other.exec(select(UserSession).where(UserSession.id == session_id))).one()
        await other.exec(update(UserSession).where(UserSession.id == session_id)
                         .values(refresh_rotated_at=session_lifetimes.utc(row.refresh_rotated_at) - ago))
        await other.commit()


async def _login(db, user):
    row = await AuthService.create_session(db, user)
    return row, AuthService.session_tokens(user.id, row)[1]


async def _refresh(client, token, **kwargs):
    return await client.post(REFRESH, json={"refresh_token": token}, **kwargs)


async def _reuse_logged(session_maker, user_id):
    async with session_maker() as fresh:
        return (await fresh.exec(select(AuditLog).where(
            AuditLog.action == "end", AuditLog.resource_id == user_id))).all()


def _bearer(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}"}


@pytest.mark.asyncio
async def test_a_refresh_records_the_previous_token_and_when(client, db, session_maker):
    user = await make_user(db)
    row, first = await _login(db, user)
    assert row.previous_refresh_jti is None and row.refresh_rotated_at is None
    response = await _refresh(client, first)
    assert response.status_code == 200, response.text
    stored = await _stored(session_maker, row.id)
    assert stored.previous_refresh_jti == jwt.get_unverified_claims(first)["jti"]
    assert stored.refresh_jti == jwt.get_unverified_claims(response.json()["refresh_token"])["jti"]
    assert stored.refresh_rotated_at is not None


@pytest.mark.asyncio
async def test_the_previous_token_within_the_grace_window_is_refused_and_the_session_stays(client, db, session_maker):
    """Two tabs refresh at the same moment with the same token: one wins, the other answers invalid_refresh_token and
    the winner's tokens go on working"""
    user = await make_user(db)
    row, first = await _login(db, user)
    winner = await _refresh(client, first)
    assert winner.status_code == 200, winner.text
    loser = await _refresh(client, first)
    assert loser.status_code == 401 and loser.json()["code"] == "invalid_refresh_token", loser.text
    assert (await _stored(session_maker, row.id)).ended_at is None
    assert (await client.get(ME, headers=_bearer(winner.json()["access_token"]))).status_code == 200
    assert (await _refresh(client, winner.json()["refresh_token"])).status_code == 200
    assert await _reuse_logged(session_maker, user.id) == []


@pytest.mark.asyncio
async def test_the_previous_token_after_the_grace_window_ends_the_session(client, db, session_maker):
    user = await make_user(db)
    row, first = await _login(db, user)
    winner = await _refresh(client, first)
    assert winner.status_code == 200, winner.text
    await _rotated_ago(session_maker, row.id,
                       timedelta(seconds=settings.security.refresh_reuse_grace_seconds + 1))

    reused = await _refresh(client, first)
    assert reused.status_code == 401 and reused.json()["code"] == "refresh_token_reused", reused.text
    assert reused.headers["WWW-Authenticate"].startswith("Bearer")
    assert reused.headers["content-type"].startswith("application/problem+json")
    stored = await _stored(session_maker, row.id)
    assert stored.ended_at is not None
    assert (stored.end_reason, stored.revoke_cause) == (SessionEndReason.REVOKED.value,
                                                        SessionRevokeCause.REFRESH_TOKEN_REUSED.value)
    # the holder of the newest tokens is out as well: the whole session ended
    after = await _refresh(client, winner.json()["refresh_token"])
    assert after.status_code == 401 and after.json()["code"] == "session_expired", after.text
    me = await client.get(ME, headers=_bearer(winner.json()["access_token"]))
    assert me.status_code == 401 and me.json()["code"] == "session_expired"

    (logged,) = await _reuse_logged(session_maker, user.id)
    assert logged.params["cause"] == "refresh_token_reused" and logged.params["ended_session_id"] == row.id
    assert logged.session_id == row.id


@pytest.mark.asyncio
async def test_an_older_token_ends_the_session_even_within_the_grace_window(client, db, session_maker):
    user = await make_user(db)
    row, first = await _login(db, user)
    second = (await _refresh(client, first)).json()["refresh_token"]
    third = await _refresh(client, second)
    assert third.status_code == 200, third.text
    reused = await _refresh(client, first)
    assert reused.status_code == 401 and reused.json()["code"] == "refresh_token_reused", reused.text
    assert (await _stored(session_maker, row.id)).revoke_cause == SessionRevokeCause.REFRESH_TOKEN_REUSED.value


@pytest.mark.asyncio
async def test_no_grace_window_ends_the_session_at_the_first_reuse(client, db, session_maker, monkeypatch):
    monkeypatch.setattr(settings.security, "refresh_reuse_grace_seconds", 0)
    user = await make_user(db)
    row, first = await _login(db, user)
    assert (await _refresh(client, first)).status_code == 200
    reused = await _refresh(client, first)
    assert reused.status_code == 401 and reused.json()["code"] == "refresh_token_reused", reused.text


@pytest.mark.asyncio
async def test_a_reuse_ends_only_its_own_session(client, db, session_maker):
    user = await make_user(db)
    row, first = await _login(db, user)
    other_row, other = await _login(db, user)
    assert (await _refresh(client, first)).status_code == 200
    await _rotated_ago(session_maker, row.id, timedelta(minutes=5))
    assert (await _refresh(client, first)).json()["code"] == "refresh_token_reused"
    assert (await _stored(session_maker, other_row.id)).ended_at is None
    assert (await _refresh(client, other)).status_code == 200


@pytest.mark.asyncio
async def test_a_session_from_before_034_answers_a_reuse_as_before(client, db, session_maker):
    """refresh_jti set (025) but no previous token recorded: a token that is not the current one is refused and the
    session stays, whatever it is (nothing tells the previous token from an older one)"""
    user = await make_user(db)
    row, current = await _login(db, user)
    assert row.previous_refresh_jti is None
    stale = AuthService.create_refresh_token(user.id, session_id=row.id)       # another jti of the session
    response = await _refresh(client, stale)
    assert response.status_code == 401 and response.json()["code"] == "invalid_refresh_token", response.text
    assert (await _stored(session_maker, row.id)).ended_at is None
    assert (await _refresh(client, current)).status_code == 200


@pytest.mark.asyncio
async def test_the_first_refresh_of_a_session_from_before_025_records_no_previous_token(client, db, session_maker):
    """Before 025 any token of a session refreshed it: its first refresh pins the newest token, and an older one
    presented after it is answered as before (invalid_refresh_token, the session stays), never as a reuse"""
    user = await make_user(db)
    row = UserSession(id="before-025", user_id=user.id,
                      expires_at=session_lifetimes._now() + timedelta(minutes=30))
    db.add(row)
    await db.commit()
    one = AuthService.create_refresh_token(user.id, session_id=row.id)
    other = AuthService.create_refresh_token(user.id, session_id=row.id)
    assert (await _refresh(client, one)).status_code == 200
    stored = await _stored(session_maker, row.id)
    assert stored.previous_refresh_jti is None and stored.refresh_rotated_at is not None
    await _rotated_ago(session_maker, row.id, timedelta(minutes=5))
    response = await _refresh(client, other)
    assert response.status_code == 401 and response.json()["code"] == "invalid_refresh_token", response.text
    assert (await _stored(session_maker, row.id)).ended_at is None


@pytest.mark.asyncio
async def test_a_refused_refresh_keeps_the_previous_token_as_it_was(client, db, session_maker):
    """A refresh refused after its claim (here: the session ended meanwhile) rolls the claim back: the previous
    token and the rotation time stay those of the last successful refresh"""
    user = await make_user(db)
    row, first = await _login(db, user)
    second = (await _refresh(client, first)).json()["refresh_token"]
    async with session_maker() as other:
        await AuthService.invalidate_all_user_sessions(other, user.id, SessionRevokeCause.ENDED_BY_ADMIN)
    before = await _stored(session_maker, row.id)
    refused = await _refresh(client, second)
    assert refused.status_code == 401 and refused.json()["code"] == "session_expired", refused.text
    after = await _stored(session_maker, row.id)
    assert (after.previous_refresh_jti, after.refresh_rotated_at, after.refresh_jti) == (
        before.previous_refresh_jti, before.refresh_rotated_at, before.refresh_jti)


def test_reuse_rule():
    now = session_lifetimes._now()
    grace = timedelta(seconds=settings.security.refresh_reuse_grace_seconds)
    row = UserSession(id="rule", user_id=1, expires_at=now + timedelta(minutes=30), refresh_jti="current",
                      previous_refresh_jti="previous", refresh_rotated_at=now - grace + timedelta(seconds=1))
    rule = session_lifetimes.refresh_reuse
    assert rule(row, "previous", now) is session_lifetimes.RefreshReuse.RACED
    assert rule(row, "older", now) is session_lifetimes.RefreshReuse.REUSED
    row.refresh_rotated_at = now - grace
    assert rule(row, "previous", now) is session_lifetimes.RefreshReuse.REUSED
    row.previous_refresh_jti, row.refresh_rotated_at = None, None
    assert rule(row, "previous", now) is session_lifetimes.RefreshReuse.UNKNOWN
    assert rule(row, "older", now) is session_lifetimes.RefreshReuse.UNKNOWN
