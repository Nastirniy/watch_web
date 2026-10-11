"""Login sessions: state computed when read, idle timeout + absolute end, reasons, administrator routes."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from jose import jwt
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.config import settings
from app.backend.enums.general import SessionEndReason, SessionRevokeCause, SessionStatus
from app.backend.enums.routes import AuthRoutes, UserRoutes
from app.backend.models.role import Role
from app.backend.models.user import User, UserSession
from app.backend.auth import sessions as session_lifetimes
from app.backend.auth.service import AuthService
from tests.helpers import make_admin, make_user

ME = V1.path(AuthRoutes.ME)
REFRESH = V1.path(AuthRoutes.REFRESH)
LOGOUT = V1.path(AuthRoutes.LOGOUT)
OWN_SESSIONS = V1.path(AuthRoutes.SESSIONS)
USER = V1.path(UserRoutes.USER_BY_ID)
SESSIONS = V1.path(UserRoutes.USER_SESSIONS)
END_ALL = V1.path(UserRoutes.USER_SESSIONS_END)
END_ONE = V1.path(UserRoutes.USER_SESSION_END)

IDLE = timedelta(minutes=settings.security.session_idle_timeout_minutes)


def now() -> datetime:
    return datetime.now(timezone.utc)


def row(last_activity_ago: timedelta, expires_in: timedelta, at: datetime = None, created_ago: timedelta = timedelta(0),
        **ended) -> UserSession:
    at = at or now()
    return UserSession(id=uuid.uuid4().hex, user_id=1, last_activity=at - last_activity_ago,
                       expires_at=at + expires_in, created_at=at - created_ago, **ended)


async def make_session(db, user, last_activity_ago=timedelta(0), expires_in=timedelta(hours=8),
                       created_ago=timedelta(0)) -> UserSession:
    session_row = row(last_activity_ago, expires_in, created_ago=created_ago)
    session_row.user_id = user.id
    db.add(session_row)
    await db.commit()
    await db.refresh(session_row)
    return session_row


def bearer(user, session_row) -> dict:
    return {"Authorization": f"Bearer {AuthService.create_access_token(user.id, session_id=session_row.id)}"}


# ------------------------------------------------------------------ computed state
def test_live_states():
    at = now()
    online = session_lifetimes.session_state(row(timedelta(seconds=10), timedelta(hours=1), at=at), at)
    assert online.status is SessionStatus.ONLINE and online.end_reason is None
    idle = session_lifetimes.session_state(row(IDLE - timedelta(minutes=1), timedelta(hours=1), at=at), at)
    assert idle.status is SessionStatus.IDLE
    assert idle.ends_at == at + timedelta(minutes=1), "ends when the idle timeout runs out unless a request comes"


def test_ended_by_the_computation_with_the_earlier_reason():
    at = now()
    quiet = session_lifetimes.session_state(row(IDLE + timedelta(minutes=1), timedelta(hours=1), at=at), at)
    assert (quiet.status, quiet.end_reason) == (SessionStatus.ENDED, SessionEndReason.IDLE_TIMEOUT)
    assert quiet.ends_at == at - timedelta(minutes=1), "ended when the idle timeout ran out, not when read"

    # a stored end before the inactivity deadline (a session from before sliding sessions) is an expiry
    past_end = session_lifetimes.session_state(row(timedelta(seconds=10), timedelta(minutes=-2), at=at), at)
    assert (past_end.status, past_end.end_reason) == (SessionStatus.ENDED, SessionEndReason.EXPIRED)
    assert past_end.ends_at == at - timedelta(minutes=2)


def test_activity_keeps_a_session_alive_unless_a_hard_end_is_set(monkeypatch):
    at = now()
    old_but_active = row(timedelta(seconds=10), IDLE, at=at, created_ago=timedelta(days=3))
    assert session_lifetimes.session_state(old_but_active, at).status is SessionStatus.ONLINE

    monkeypatch.setattr(settings.security, "session_max_hours", 1)
    state = session_lifetimes.session_state(old_but_active, at)
    assert (state.status, state.end_reason) == (SessionStatus.ENDED, SessionEndReason.EXPIRED)
    assert state.ends_at == at - timedelta(days=3) + timedelta(hours=1)


def test_touch_moves_the_end_never_past_the_hard_end(monkeypatch):
    at = now()
    session_row = row(timedelta(minutes=10), timedelta(minutes=20), at=at, created_ago=timedelta(minutes=50))
    session_lifetimes.touch(session_row, at)
    assert session_row.last_activity == at and session_row.expires_at == at + IDLE

    monkeypatch.setattr(settings.security, "session_max_hours", 1)
    session_lifetimes.touch(session_row, at)
    assert session_row.expires_at == at + timedelta(minutes=10), "login 50 minutes ago + 1 hour"


def test_stored_end_wins():
    at = now()
    revoked = row(timedelta(seconds=10), timedelta(hours=1), ended_at=at, end_reason="revoked", revoke_cause="ended_by_admin")
    state = session_lifetimes.session_state(revoked, at)
    assert (state.status, state.end_reason, state.revoke_cause) == (
        SessionStatus.ENDED, SessionEndReason.REVOKED, SessionRevokeCause.ENDED_BY_ADMIN)


# ------------------------------------------------------------------ token checks
@pytest.mark.asyncio
async def test_idle_session_is_refused_and_recorded(client, db):
    user = await make_user(db)
    quiet = await make_session(db, user, last_activity_ago=IDLE + timedelta(minutes=5))
    response = await client.get(ME, headers=bearer(user, quiet))
    assert response.status_code == 401 and response.json()["code"] == "session_expired"
    await db.refresh(quiet)
    assert quiet.end_reason == "idle_timeout"
    assert abs((session_lifetimes.utc(quiet.ended_at) - (now() - timedelta(minutes=5))).total_seconds()) < 5


@pytest.mark.asyncio
async def test_a_request_moves_the_end_of_a_long_running_session(client, db):
    """No hard end by default: a session opened days ago lives on while it is used"""
    user = await make_user(db)
    session_row = await make_session(db, user, last_activity_ago=timedelta(minutes=2), expires_in=IDLE - timedelta(minutes=2),
                                     created_ago=timedelta(days=3))
    response = await client.get(ME, headers=bearer(user, session_row))
    assert response.status_code == 200, response.text
    await db.refresh(session_row)
    assert abs((session_lifetimes.utc(session_row.expires_at) - (now() + IDLE)).total_seconds()) < 5


@pytest.mark.asyncio
async def test_refresh_is_activity_and_renews_both_tokens(client, db):
    user = await make_user(db)
    session_row = await make_session(db, user, last_activity_ago=IDLE - timedelta(minutes=2), expires_in=timedelta(minutes=2),
                                     created_ago=timedelta(days=1))
    refresh_token = AuthService.create_refresh_token(user.id, session_id=session_row.id)

    response = await client.post(REFRESH, json={"refresh_token": refresh_token})
    assert response.status_code == 200, response.text
    body = response.json()
    await db.refresh(session_row)
    assert now() - session_lifetimes.utc(session_row.last_activity) < timedelta(seconds=30), "a refresh is activity"
    assert abs((session_lifetimes.utc(session_row.expires_at) - (now() + IDLE)).total_seconds()) < 5, "the end moved"

    claims = jwt.get_unverified_claims(body["refresh_token"])
    assert claims["sid"] == session_row.id
    window = AuthService.refresh_window().total_seconds()
    assert abs(claims["exp"] - (now().timestamp() + window)) < 5, "usable until after the access token, plus the idle timeout"
    assert body["expires_in"] == settings.security.access_token_expire_minutes * 60


@pytest.mark.asyncio
async def test_tokens_never_outlive_a_hard_end(db, monkeypatch):
    monkeypatch.setattr(settings.security, "session_max_hours", 1)
    user = await make_user(db)
    late = await make_session(db, user, created_ago=timedelta(minutes=55))
    access, refresh, expires_in = AuthService.session_tokens(user.id, late)
    hard_end = int((session_lifetimes.utc(late.created_at) + timedelta(hours=1)).timestamp())
    assert 0 < expires_in <= 300
    assert jwt.get_unverified_claims(access)["exp"] <= hard_end
    assert jwt.get_unverified_claims(refresh)["exp"] <= hard_end


@pytest.mark.asyncio
async def test_refresh_token_without_session_is_refused(client, db):
    user = await make_user(db)
    response = await client.post(REFRESH, json={"refresh_token": AuthService.create_refresh_token(user.id)})
    assert response.status_code == 401 and response.json()["code"] == "invalid_refresh_token"


@pytest.mark.asyncio
async def test_logout_records_the_reason(client, db):
    user = await make_user(db)
    session_row = await make_session(db, user)
    response = await client.post(LOGOUT, json={}, headers=bearer(user, session_row))
    assert response.status_code == 200, response.text
    await db.refresh(session_row)
    assert session_row.end_reason == "logout" and session_row.ended_at is not None
    assert (await client.get(ME, headers=bearer(user, session_row))).json()["code"] == "session_expired"


@pytest.mark.asyncio
async def test_logout_needs_no_body(client, db):
    """The token names its session: the Authorization header is all a logout needs"""
    user = await make_user(db)
    session_row = await make_session(db, user)
    response = await client.post(LOGOUT, headers=bearer(user, session_row))
    assert response.status_code == 200, response.text
    await db.refresh(session_row)
    assert session_row.end_reason == "logout"


@pytest.mark.asyncio
async def test_headers_fixture_logs_in_with_a_real_session(client, db, headers):
    """headers(user) of tests/conftest.py: a stored live session and a token bound to it, a new one per call"""
    user = await make_user(db)
    first, second = headers(user), headers(user)
    sid = jwt.get_unverified_claims(first["Authorization"].removeprefix("Bearer "))["sid"]
    body = (await client.get(OWN_SESSIONS, headers=first)).json()
    assert sid in {s["id"] for s in body["sessions"]} and len(body["sessions"]) == 2
    assert (await client.post(LOGOUT, headers=first)).status_code == 200
    assert (await client.get(ME, headers=first)).json()["code"] == "session_expired"
    assert (await client.get(ME, headers=second)).status_code == 200


@pytest.mark.asyncio
async def test_own_sessions_list_only_live_ones(client, db):
    user = await make_user(db)
    live = await make_session(db, user)
    await make_session(db, user, last_activity_ago=IDLE + timedelta(minutes=1))
    body = (await client.get(OWN_SESSIONS, headers=bearer(user, live))).json()
    assert [s["id"] for s in body["sessions"]] == [live.id]
    assert body["sessions"][0]["status"] == "online"
    assert body["sessions"][0]["expires_at"].endswith("Z"), "UTC with the offset (the shared response rule)"


# ------------------------------------------------------------------ administrator routes
@pytest.mark.asyncio
async def test_admin_sees_live_and_ended_sessions_with_reasons(client, db, headers):
    admin = await make_admin(db)
    user = await make_user(db)
    online = await make_session(db, user)
    idle = await make_session(db, user, last_activity_ago=IDLE - timedelta(minutes=5))
    quiet = await make_session(db, user, last_activity_ago=IDLE + timedelta(minutes=5))

    live = (await client.get(SESSIONS.format(user_id=user.id), headers=headers(admin))).json()
    assert {s["id"]: s["status"] for s in live} == {online.id: "online", idle.id: "idle"}
    assert all(s["expires_at"] and s["end_reason"] is None and s["ended_at"] is None for s in live)

    everything = (await client.get(SESSIONS.format(user_id=user.id), params={"include_ended": True}, headers=headers(admin))).json()
    ended = {s["id"]: s for s in everything if s["status"] == "ended"}
    assert set(ended) == {quiet.id} and ended[quiet.id]["end_reason"] == "idle_timeout"
    assert ended[quiet.id]["revoke_cause"] is None and ended[quiet.id]["ended_at"]

    missing = await client.get(SESSIONS.format(user_id=999999), headers=headers(admin))
    assert missing.status_code == 404 and missing.json()["code"] == "user_not_found"


@pytest.mark.asyncio
async def test_admin_ends_one_session(client, db, headers):
    admin = await make_admin(db)
    user = await make_user(db)
    target = await make_session(db, user)
    other = await make_session(db, user)

    response = await client.post(END_ONE.format(user_id=user.id, session_id=target.id), headers=headers(admin))
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["status"], body["end_reason"], body["revoke_cause"]) == ("ended", "revoked", "ended_by_admin")
    assert (await client.get(ME, headers=bearer(user, target))).status_code == 401
    assert (await client.get(ME, headers=bearer(user, other))).status_code == 200, "only that session ends"

    again = await client.post(END_ONE.format(user_id=user.id, session_id=target.id), headers=headers(admin))
    assert again.status_code == 409 and again.json()["code"] == "session_already_ended"
    assert again.json()["end_reason"] == "revoked"
    unknown = await client.post(END_ONE.format(user_id=user.id, session_id="nope"), headers=headers(admin))
    assert unknown.status_code == 404 and unknown.json()["code"] == "session_not_found"


@pytest.mark.asyncio
async def test_admin_ends_all_sessions_the_account_stays(client, db, headers):
    admin = await make_admin(db)
    user = await make_user(db)
    first, second = await make_session(db, user), await make_session(db, user)
    await make_session(db, user, expires_in=timedelta(minutes=-1))  # past its end: recorded as expired, not revoked

    response = await client.post(END_ALL.format(user_id=user.id), headers=headers(admin))
    assert response.status_code == 200 and response.json() == {"ended_count": 2}
    for session_row in (first, second):
        assert (await client.get(ME, headers=bearer(user, session_row))).json()["code"] == "session_expired"
    user_id = user.id
    db.expire_all()  # the API changed the rows through its own session
    reasons = sorted((r.end_reason, r.revoke_cause or "") for r in (await db.exec(
        select(UserSession).where(UserSession.user_id == user_id))).all())
    assert reasons == [("expired", ""), ("revoked", "ended_by_admin"), ("revoked", "ended_by_admin")]

    db.expire_all()
    account = (await db.exec(select(User).where(User.id == user_id))).one()
    assert account.is_active and not account.is_deleted
    fresh = await make_session(db, account)
    assert (await client.get(ME, headers=bearer(user, fresh))).status_code == 200, "the user can log in again"


# ------------------------------------------------------------------ PATCH /users/{id}
@pytest.mark.asyncio
async def test_refused_role_leaves_the_patch_unsaved(client, db, headers):
    admin_headers = headers(await make_admin(db))
    user = await make_user(db)
    user_id, username = user.id, user.username
    archived_role = Role(name="old_role", display_name="Old", is_deleted=True)
    db.add(archived_role)
    await db.commit()
    await db.refresh(archived_role)

    for role_id, code in ((999999, "role_not_found"), (archived_role.id, "role_archived_not_assignable")):
        response = await client.patch(USER.format(user_id=user_id), headers=admin_headers,
                                      data={"username": "renamed_user", "role_ids": [role_id]})
        assert response.json()["code"] == code, response.text
        db.expire_all()
        assert (await db.exec(select(User).where(User.id == user_id))).one().username == username, \
            "a refused role leaves the other fields unsaved"
