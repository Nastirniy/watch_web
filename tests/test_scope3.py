"""Scope 3: session expiry, role permission toggle, statistics, archive / restore of observations and users."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select

from app.backend.enums.routes import (
    AuthRoutes, RoleRoutes, StatisticsRoutes, ObservationRoutes, UserRoutes, PostRoutes, Routes,
)
from app.backend.models.user import UserSession
from app.backend.models.role import Permission
from app.backend.services.auth import AuthService
from tests.helpers import make_post, make_user, make_admin, make_role, make_dictionary, make_observation, make_media

ME = AuthRoutes.ME.versioned_path()
LIST = ObservationRoutes.LIST_ALL.versioned_path()
OBS = ObservationRoutes.OBSERVATION_BY_ID.versioned_path()
OBS_ARCHIVE = ObservationRoutes.OBSERVATION_ARCHIVE.versioned_path()
OBS_RESTORE = ObservationRoutes.OBSERVATION_RESTORE.versioned_path()
USER = UserRoutes.USER_BY_ID.versioned_path()
USER_ARCHIVE = UserRoutes.USER_ARCHIVE.versioned_path()
USER_RESTORE = UserRoutes.USER_RESTORE.versioned_path()
USERS = UserRoutes.ROOT.versioned_path()


async def make_session(db, user, expires_in: timedelta) -> UserSession:
    row = UserSession(id=uuid.uuid4().hex, user_id=user.id, expires_at=datetime.now(timezone.utc) + expires_in,
                      last_activity=datetime.now(timezone.utc))
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


def bearer(user, user_session) -> dict:
    return {"Authorization": f"Bearer {AuthService.create_access_token(user.id, session_id=user_session.id)}"}


# ----------------------------------------------------------------- dead enum members
def test_dead_route_members_are_gone():
    assert not hasattr(AuthRoutes, "CHANGE_PASSWORD")
    assert not hasattr(AuthRoutes, "CERTIFICATE_VALIDATE")
    assert not hasattr(PostRoutes, "STATISTICS")
    assert not hasattr(ObservationRoutes, "BULK_UPDATE")
    assert not hasattr(Routes, "EXPORTS") and not hasattr(Routes, "SIGNALS")
    assert Routes.STATISTICS is StatisticsRoutes
    assert StatisticsRoutes.BASE.versioned_path() == "/api/v1/statistics"


# ----------------------------------------------------------------- session expiry
@pytest.mark.asyncio
async def test_expired_session_is_rejected_and_closed(client, db):
    user = await make_user(db)
    expired = await make_session(db, user, timedelta(minutes=-1))
    response = await client.get(ME, headers=bearer(user, expired))
    assert response.status_code == 401
    assert response.json()["code"] == "session_expired"
    row = (await db.exec(select(UserSession).where(UserSession.id == expired.id))).one()
    await db.refresh(row)
    assert row.is_active is False and row.ended_at is not None

    valid = await make_session(db, user, timedelta(hours=1))
    assert (await client.get(ME, headers=bearer(user, valid))).status_code == 200


@pytest.mark.asyncio
async def test_refresh_rejects_expired_session(client, db):
    user = await make_user(db)
    expired = await make_session(db, user, timedelta(minutes=-1))
    refresh_token = AuthService.create_refresh_token(user.id, session_id=expired.id)
    response = await client.post(AuthRoutes.REFRESH.versioned_path(), json={"refresh_token": refresh_token})
    assert response.status_code == 401
    assert response.json()["code"] == "session_expired"


# ----------------------------------------------------------------- role permission toggle
@pytest.mark.asyncio
async def test_role_permission_toggle(client, db, headers):
    admin = await make_admin(db)
    role = await make_role(db, [])
    user = await make_user(db, role=role)
    permission = (await db.exec(select(Permission).where(Permission.name == "tag.view"))).one()
    url = RoleRoutes.ROLE_PERMISSION_BY_ID.versioned_path().format(role_id=role.id, permission_id=permission.id)

    added = await client.put(url, headers=headers(admin))
    assert added.status_code == 200, added.text
    assert [p["name"] for p in added.json()] == ["tag.view"]
    again = await client.put(url, headers=headers(admin))
    assert [p["name"] for p in again.json()] == ["tag.view"]
    assert "tag.view" in (await client.get(ME, headers=headers(user))).json()["permissions"]

    removed = await client.delete(url, headers=headers(admin))
    assert removed.status_code == 200 and removed.json() == []
    assert (await client.delete(url, headers=headers(admin))).status_code == 404
    assert "tag.view" not in (await client.get(ME, headers=headers(user))).json()["permissions"]

    not_admin = await client.put(url, headers=headers(user))
    assert not_admin.status_code == 403


# ----------------------------------------------------------------- statistics
@pytest.mark.asyncio
async def test_statistics_endpoint(client, db, headers):
    post = await make_post(db)
    ids = await make_dictionary(db)
    admin = await make_admin(db, post_id=post.id)
    user = await make_user(db, perms=["statistics.view"], post_id=post.id)
    observation = await make_observation(db, user, post, ids["signal_type_id"])
    await make_media(db, observation, user)
    await make_session(db, user, timedelta(hours=1))

    url = StatisticsRoutes.ROOT.versioned_path()
    no_perm = await make_user(db, perms=["observation.view"], post_id=post.id)
    assert (await client.get(url, headers=headers(no_perm))).status_code == 403

    response = await client.get(url, headers=headers(admin))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["post_ids"] is None
    assert body["posts"] == {"total": 1, "active": 1, "online": 1}
    assert body["users"]["total"] == 3 and body["users"]["online"] == 1 and body["users"]["deleted"] == 0
    assert body["observations"]["total_in_period"] == 1
    assert body["observations"]["by_status"] == {"active": 1}
    assert body["observations"]["by_post"] == [{"post_id": post.id, "post_name": post.name, "count": 1}]
    assert body["media"]["count"] == 1 and body["media"]["total_size_bytes"] == 123
    assert body["media"]["posters"] == {"pending": 0, "processing": 0, "ready": 0, "failed": 0, "untracked": 0}
    assert body["period"]["date_from"] < body["period"]["date_to"]

    bad = await client.get(url, params={"date_from": "2030-01-01T00:00:00", "date_to": "2020-01-01T00:00:00"}, headers=headers(admin))
    assert bad.status_code == 400

    # Without admin.true the figures cover the caller's post scope only
    other_post = await make_post(db)
    other_user = await make_user(db, perms=["observation.view"], post_id=other_post.id)
    other_observation = await make_observation(db, other_user, other_post, ids["signal_type_id"])
    await make_media(db, other_observation, other_user, media_type="video")

    scoped = await client.get(url, headers=headers(user))
    assert scoped.status_code == 200, scoped.text
    scoped_body = scoped.json()
    assert scoped_body["post_ids"] == [post.id]
    assert scoped_body["posts"]["total"] == 1
    assert scoped_body["users"]["total"] == 3
    assert scoped_body["observations"]["by_post"] == [{"post_id": post.id, "post_name": post.name, "count": 1}]
    assert scoped_body["media"]["count"] == 1 and scoped_body["media"]["posters"]["pending"] == 0

    everything = (await client.get(url, headers=headers(admin))).json()
    assert everything["posts"]["total"] == 2 and everything["users"]["total"] == 4
    assert {row["post_id"] for row in everything["observations"]["by_post"]} == {post.id, other_post.id}
    assert everything["media"]["count"] == 2 and everything["media"]["posters"]["pending"] == 1



# ----------------------------------------------------------------- observation archive / restore
@pytest.mark.asyncio
async def test_observation_archive_and_restore(client, db, headers):
    post = await make_post(db)
    ids = await make_dictionary(db)
    owner = await make_user(db, perms=["observation.view", "observation.update", "observation.archive"], post_id=post.id)
    viewer = await make_user(db, perms=["observation.view", "observation.update"], post_id=post.id)
    observation = await make_observation(db, owner, post, ids["signal_type_id"])
    url = OBS.format(observation_id=observation.id)
    archive = OBS_ARCHIVE.format(observation_id=observation.id)
    restore = OBS_RESTORE.format(observation_id=observation.id)

    assert (await client.delete(url, headers=headers(owner))).status_code == 405, "observations have no hard delete"
    no_perm = await client.post(archive, headers=headers(viewer))
    assert no_perm.status_code == 403 and no_perm.json()["missing"] == ["observation.archive"]
    assert (await client.post(restore, headers=headers(owner))).status_code == 409, "not archived yet"

    archived = await client.post(archive, headers=headers(owner))
    assert archived.status_code == 200, archived.text
    assert archived.json()["id"] == observation.id and archived.json()["is_deleted"] is True
    assert (await client.post(archive, headers=headers(owner))).status_code == 404

    assert (await client.get(LIST, headers=headers(owner))).json() == []
    hidden_for_viewer = await client.get(LIST, params={"include_deleted": "true"}, headers=headers(viewer))
    assert hidden_for_viewer.status_code == 403
    assert hidden_for_viewer.json()["missing"] == ["observation.archive"]
    with_deleted = (await client.get(LIST, params={"include_deleted": "true"}, headers=headers(owner))).json()
    assert [o["id"] for o in with_deleted] == [observation.id] and with_deleted[0]["is_deleted"] is True

    # archived observations cannot be edited, only restored; is_deleted in the body is ignored
    assert (await client.put(url, json={"comment": "x"}, headers=headers(owner))).status_code == 404
    assert (await client.put(url, json={"is_deleted": False}, headers=headers(owner))).status_code == 404
    viewer_restore = await client.post(restore, headers=headers(viewer))
    assert viewer_restore.status_code == 403
    assert viewer_restore.json()["code"] in ("observation_not_owner", "permission_denied")
    restored = await client.post(restore, headers=headers(owner))
    assert restored.status_code == 200 and restored.json()["is_deleted"] is False
    assert [o["id"] for o in (await client.get(LIST, headers=headers(owner))).json()] == [observation.id]

    from app.backend.models.audit import AuditLog
    actions = (await db.exec(select(AuditLog.action).where(AuditLog.resource_type == "observation"))).all()
    assert {"archive", "restore"} <= set(actions)


# ----------------------------------------------------------------- user archive / restore
@pytest.mark.asyncio
async def test_user_archive_and_restore(client, db, headers):
    post = await make_post(db)
    ids = await make_dictionary(db)
    admin = await make_admin(db, post_id=post.id)
    victim = await make_user(db, perms=["observation.view"], post_id=post.id)
    observation = await make_observation(db, victim, post, ids["signal_type_id"])
    live = await make_session(db, victim, timedelta(hours=1))
    assert (await client.get(ME, headers=bearer(victim, live))).status_code == 200

    assert (await client.delete(USER.format(user_id=victim.id), headers=headers(admin))).status_code == 405, "users have no hard delete"
    assert (await client.post(USER_ARCHIVE.format(user_id=admin.id), headers=headers(admin))).status_code == 400
    deleted = await client.post(USER_ARCHIVE.format(user_id=victim.id), headers=headers(admin))
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["is_deleted"] is True and deleted.json()["is_active"] is False
    assert (await client.post(USER_ARCHIVE.format(user_id=victim.id), headers=headers(admin))).status_code == 404

    # sessions invalidated, login blocked, hidden from lists, creator info still shows the user as deleted
    blocked = await client.get(ME, headers=bearer(victim, live))
    assert blocked.status_code == 401 and blocked.json()["code"] in ("account_deleted", "session_expired")
    listed = (await client.get(USERS, headers=headers(admin))).json()
    assert victim.id not in {u["id"] for u in listed}
    with_deleted = (await client.get(USERS, params={"include_deleted": "true"}, headers=headers(admin))).json()
    assert {u["id"]: u["is_deleted"] for u in with_deleted}[victim.id] is True
    creator = (await client.get(LIST, headers=headers(admin))).json()[0]["user"]
    assert creator["id"] == victim.id and creator["is_deleted"] is True

    # archived users are read-only until restored
    assert (await client.patch(USER.format(user_id=victim.id), data={"is_deleted": "false"}, headers=headers(admin))).status_code == 404
    restored = await client.post(USER_RESTORE.format(user_id=victim.id), headers=headers(admin))
    assert restored.status_code == 200, restored.text
    assert restored.json()["is_deleted"] is False and restored.json()["is_active"] is True
    assert (await client.post(USER_RESTORE.format(user_id=victim.id), headers=headers(admin))).status_code == 409
    assert victim.id in {u["id"] for u in (await client.get(USERS, headers=headers(admin))).json()}
