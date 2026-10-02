"""Tokens of a deactivated or archived user stop working at once, whatever way the flag changed."""
import pytest
from sqlmodel import select

from app.backend.enums.routes import AuthRoutes, UserRoutes
from app.backend.api.v1 import V1
from app.backend.models.user import UserSession
from app.backend.services.auth import AuthService
from tests.helpers import make_user, make_admin

ME = V1.path(AuthRoutes.ME)
REFRESH = V1.path(AuthRoutes.REFRESH)
USER = V1.path(UserRoutes.USER_BY_ID)
ARCHIVE = V1.path(UserRoutes.USER_ARCHIVE)


async def login(db, user):
    """A real session (like POST /auth/login) with tokens bound to it."""
    user_session = await AuthService.create_session(db, user)
    access = AuthService.create_access_token(user.id, session_id=user_session.id)
    refresh = AuthService.create_refresh_token(user.id, session_id=user_session.id)
    return user_session, {"Authorization": f"Bearer {access}"}, refresh


async def live_sessions(db, user_id: int):
    result = await db.exec(select(UserSession).where(UserSession.user_id == user_id, UserSession.is_active == True))
    return result.all()


@pytest.mark.asyncio
async def test_deactivation_ends_sessions_and_rejects_the_token(client, db, headers):
    admin = await make_admin(db)
    victim = await make_user(db)
    victim_id = victim.id
    first, victim_headers, victim_refresh = await login(db, victim)
    second, _, _ = await login(db, victim)
    assert (await client.get(ME, headers=victim_headers)).status_code == 200

    response = await client.patch(USER.format(user_id=victim_id), data={"is_active": "false"}, headers=headers(admin))
    assert response.status_code == 200, response.text
    assert response.json()["is_active"] is False

    # Both sessions were ended together with the flag change
    db.expire_all()
    assert await live_sessions(db, victim_id) == []
    for user_session in (first, second):
        await db.refresh(user_session)
        assert user_session.is_active is False
        assert user_session.ended_at is not None

    # The access token is dead, with the reason the client needs
    response = await client.get(ME, headers=victim_headers)
    assert response.status_code == 401
    assert response.json()["code"] == "account_deactivated"

    # The refresh token cannot bring it back
    response = await client.post(REFRESH, json={"refresh_token": victim_refresh})
    assert response.status_code == 401
    assert response.json()["code"] == "account_deactivated"


@pytest.mark.asyncio
async def test_flag_changed_outside_the_api_still_expires_the_session(client, db):
    """A session left live by a direct database change is ended on the next request with its token."""
    victim = await make_user(db)
    user_session, victim_headers, victim_refresh = await login(db, victim)

    victim.is_active = False
    db.add(victim)
    await db.commit()
    assert len(await live_sessions(db, victim.id)) == 1

    response = await client.get(ME, headers=victim_headers)
    assert response.status_code == 401
    assert response.json()["code"] == "account_deactivated"

    db.expire_all()
    await db.refresh(user_session)
    assert user_session.is_active is False
    assert user_session.ended_at is not None


@pytest.mark.asyncio
async def test_refresh_of_inactive_user_ends_live_session(client, db):
    victim = await make_user(db)
    user_session, _, victim_refresh = await login(db, victim)
    victim.is_active = False
    db.add(victim)
    await db.commit()

    response = await client.post(REFRESH, json={"refresh_token": victim_refresh})
    assert response.status_code == 401
    assert response.json()["code"] == "account_deactivated"
    db.expire_all()
    await db.refresh(user_session)
    assert user_session.is_active is False


@pytest.mark.asyncio
async def test_archive_rejects_the_token_as_deleted(client, db, headers):
    admin = await make_admin(db)
    victim = await make_user(db)
    user_session, victim_headers, victim_refresh = await login(db, victim)

    assert (await client.post(ARCHIVE.format(user_id=victim.id), headers=headers(admin))).status_code == 200
    db.expire_all()
    await db.refresh(user_session)
    assert user_session.is_active is False

    response = await client.get(ME, headers=victim_headers)
    assert response.status_code == 401
    assert response.json()["code"] == "account_deleted"
    response = await client.post(REFRESH, json={"refresh_token": victim_refresh})
    assert response.status_code == 401
    assert response.json()["code"] == "account_deleted"


@pytest.mark.asyncio
async def test_reactivation_does_not_revive_old_sessions(client, db, headers):
    admin = await make_admin(db)
    victim = await make_user(db)
    _, old_headers, _ = await login(db, victim)

    assert (await client.patch(USER.format(user_id=victim.id), data={"is_active": "false"}, headers=headers(admin))).status_code == 200
    assert (await client.patch(USER.format(user_id=victim.id), data={"is_active": "true"}, headers=headers(admin))).status_code == 200

    # The old session stays ended: a new login is required
    response = await client.get(ME, headers=old_headers)
    assert response.status_code == 401
    assert response.json()["code"] == "session_expired"

    db.expire_all()
    await db.refresh(victim)
    _, new_headers, _ = await login(db, victim)
    assert (await client.get(ME, headers=new_headers)).status_code == 200


@pytest.mark.asyncio
async def test_deactivating_an_already_inactive_user_is_idempotent(client, db, headers):
    admin = await make_admin(db)
    victim = await make_user(db)
    assert (await client.patch(USER.format(user_id=victim.id), data={"is_active": "false"}, headers=headers(admin))).status_code == 200
    response = await client.patch(USER.format(user_id=victim.id), data={"is_active": "false", "username_alias": "Renamed"}, headers=headers(admin))
    assert response.status_code == 200, response.text
    assert response.json()["is_active"] is False
    assert response.json()["username_alias"] == "Renamed"


@pytest.mark.asyncio
async def test_admin_cannot_deactivate_own_account(client, db, headers):
    admin = await make_admin(db)
    response = await client.patch(USER.format(user_id=admin.id), data={"is_active": "false"}, headers=headers(admin))
    assert response.status_code == 400
    assert (await client.get(ME, headers=headers(admin))).status_code == 200
