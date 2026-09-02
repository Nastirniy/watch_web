"""User presence (online/offline) and the derived post connection status."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select

from app.backend.config import settings
from app.backend.enums.routes import AuthRoutes, UserRoutes, PostRoutes, ObservationRoutes
from app.backend.models.user import UserSession
from app.backend.services.auth import AuthService
from tests.helpers import make_post, make_user, make_admin, make_dictionary, make_observation

ME = AuthRoutes.ME.versioned_path()
USERS = UserRoutes.BASE.versioned_path() + "/"
POSTS = PostRoutes.BASE.versioned_path() + "/"
POST = PostRoutes.POST_BY_ID.versioned_path()


async def make_session(db, user, last_activity: datetime) -> UserSession:
    row = UserSession(
        id=uuid.uuid4().hex,
        user_id=user.id,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=8),
        last_activity=last_activity,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


def session_headers(user, user_session) -> dict:
    return {"Authorization": f"Bearer {AuthService.create_access_token(user.id, session_id=user_session.id)}"}


@pytest.mark.asyncio
async def test_request_touches_session_and_me_reports_presence(client, db):
    user = await make_user(db)
    old = datetime.now(timezone.utc) - timedelta(minutes=30)
    user_session = await make_session(db, user, old)

    response = await client.get(ME, headers=session_headers(user, user_session))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["online_threshold_seconds"] == settings.security.online_threshold_seconds
    assert body["last_activity_at"] is not None

    refreshed = (await db.exec(select(UserSession).where(UserSession.id == user_session.id))).one()
    await db.refresh(refreshed)
    touched = refreshed.last_activity
    touched = touched if touched.tzinfo else touched.replace(tzinfo=timezone.utc)
    assert datetime.now(timezone.utc) - touched < timedelta(seconds=30)


@pytest.mark.asyncio
async def test_touch_is_throttled(client, db):
    user = await make_user(db)
    recent = datetime.now(timezone.utc) - timedelta(seconds=5)
    user_session = await make_session(db, user, recent)
    await client.get(ME, headers=session_headers(user, user_session))
    row = (await db.exec(select(UserSession).where(UserSession.id == user_session.id))).one()
    await db.refresh(row)
    stored = row.last_activity if row.last_activity.tzinfo else row.last_activity.replace(tzinfo=timezone.utc)
    assert abs((stored - recent).total_seconds()) < 1, "a session touched 5 seconds ago must not be rewritten"


@pytest.mark.asyncio
async def test_user_list_presence_and_filter(client, db, headers):
    post = await make_post(db)
    viewer = await make_user(db, perms=["user.view"], post_id=post.id)
    online = await make_user(db, post_id=post.id)
    offline = await make_user(db, post_id=post.id)
    never = await make_user(db, post_id=post.id)
    await make_session(db, online, datetime.now(timezone.utc))
    await make_session(db, offline, datetime.now(timezone.utc) - timedelta(hours=2))

    listed = (await client.get(USERS, headers=headers(viewer))).json()
    by_id = {u["id"]: u for u in listed}
    assert by_id[online.id]["is_online"] is True
    assert by_id[online.id]["last_activity_at"] is not None
    assert by_id[offline.id]["is_online"] is False
    assert by_id[offline.id]["last_activity_at"] is not None
    assert by_id[never.id]["is_online"] is False
    assert by_id[never.id]["last_activity_at"] is None

    online_only = (await client.get(USERS, params={"is_online": "true"}, headers=headers(viewer))).json()
    assert {u["id"] for u in online_only} == {online.id}
    offline_only = (await client.get(USERS, params={"is_online": "false"}, headers=headers(viewer))).json()
    assert {u["id"] for u in offline_only} >= {offline.id, never.id, viewer.id}
    assert online.id not in {u["id"] for u in offline_only}

    sorted_list = await client.get(USERS, params={"sort_by": "last_activity_at", "sort_order": "descending"}, headers=headers(viewer))
    assert sorted_list.status_code == 200, sorted_list.text


@pytest.mark.asyncio
async def test_logout_makes_user_offline(client, db, headers):
    post = await make_post(db)
    viewer = await make_user(db, perms=["user.view"], post_id=post.id)
    user = await make_user(db, post_id=post.id)
    user_session = await make_session(db, user, datetime.now(timezone.utc))
    assert (await client.get(USERS, params={"is_online": "true"}, headers=headers(viewer))).json()[0]["id"] == user.id

    logout = await client.post(AuthRoutes.LOGOUT.versioned_path(), json={"session_id": user_session.id},
                               headers=session_headers(user, user_session))
    assert logout.status_code == 200, logout.text
    assert (await client.get(USERS, params={"is_online": "true"}, headers=headers(viewer))).json() == []


@pytest.mark.asyncio
async def test_creator_projection_has_presence(client, db, headers):
    post = await make_post(db)
    ids = await make_dictionary(db)
    creator = await make_user(db, perms=["observation.view"], post_id=post.id)
    await make_session(db, creator, datetime.now(timezone.utc))
    await make_observation(db, creator, post, ids["signal_type_id"])
    listed = (await client.get(ObservationRoutes.LIST_ALL.versioned_path(), headers=headers(creator))).json()
    assert listed[0]["user"]["is_online"] is True
    assert listed[0]["user"]["last_activity_at"] is not None


@pytest.mark.asyncio
async def test_post_status_is_derived_from_home_users(client, db, headers):
    post = await make_post(db)
    other = await make_post(db)
    admin = await make_admin(db)
    user = await make_user(db, perms=["post.view"], post_id=post.id)

    body = (await client.get(POST.format(post_id=post.id), headers=headers(user))).json()
    assert body["connection_status"] == "offline"
    assert body["online_user_count"] == 0
    assert body["stored_connection_status"] == "offline"

    await make_session(db, user, datetime.now(timezone.utc))
    body = (await client.get(POST.format(post_id=post.id), headers=headers(user))).json()
    assert body["connection_status"] == "online"
    assert body["online_user_count"] == 1
    assert body["last_connection_at"] is not None

    online_posts = (await client.get(POSTS, params={"connection_status": "online"}, headers=headers(admin))).json()
    assert {p["id"] for p in online_posts} == {post.id}
    offline_posts = (await client.get(POSTS, params={"connection_status": "offline"}, headers=headers(admin))).json()
    assert other.id in {p["id"] for p in offline_posts} and post.id not in {p["id"] for p in offline_posts}

    # manual override is stored but online presence wins while a user is active
    update = await client.put(POST.format(post_id=post.id), json={"connection_status": "maintenance"}, headers=headers(admin))
    assert update.status_code == 200, update.text
    assert update.json()["stored_connection_status"] == "maintenance"
    assert update.json()["connection_status"] == "online"

    rejected = await client.put(POST.format(post_id=post.id), json={"connection_status": "online"}, headers=headers(admin))
    assert rejected.status_code == 422

    maintenance_posts = (await client.get(POSTS, params={"connection_status": "maintenance"}, headers=headers(admin))).json()
    assert post.id not in {p["id"] for p in maintenance_posts}

    sorted_posts = await client.get(POSTS, params={"sort_by": "online_user_count", "sort_order": "descending"}, headers=headers(admin))
    assert sorted_posts.status_code == 200, sorted_posts.text
