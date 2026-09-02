"""Ownership: observation.manage_any and media.manage_any."""
from datetime import datetime, timezone

import pytest
from sqlmodel import select

from app.backend.enums.routes import ObservationRoutes, MediaRoutes
from app.backend.models.role import Permission, RolePermission, UserRole
from tests.helpers import make_post, make_user, make_dictionary, make_observation, make_media

UPDATE = ObservationRoutes.OBSERVATION_BY_ID.versioned_path()
DISAPPEARED = ObservationRoutes.MARK_DISAPPEARED.versioned_path()
OPERATOR = ["observation.view", "observation.update", "media.view", "media.delete"]


async def grant(db, user, permission_name: str):
    role_id = (await db.exec(select(UserRole.role_id).where(UserRole.user_id == user.id))).first()
    permission = (await db.exec(select(Permission).where(Permission.name == permission_name))).one()
    db.add(RolePermission(role_id=role_id, permission_id=permission.id))
    await db.commit()


@pytest.mark.asyncio
async def test_only_owner_or_manage_any_can_update(client, db, headers):
    post = await make_post(db)
    ids = await make_dictionary(db)
    owner = await make_user(db, perms=OPERATOR, post_id=post.id)
    colleague = await make_user(db, perms=OPERATOR, post_id=post.id)
    observation = await make_observation(db, owner, post, ids["signal_type_id"])
    url = UPDATE.format(observation_id=observation.id)

    own = await client.put(url, json={"comment": "mine"}, headers=headers(owner))
    assert own.status_code == 200, own.text
    assert own.json()["user"]["id"] == owner.id

    denied = await client.put(url, json={"comment": "theirs"}, headers=headers(colleague))
    assert denied.status_code == 403
    detail = denied.json()["detail"]
    assert detail["code"] == "not_owner"
    assert detail["required"]["all"] == ["observation.manage_any"]
    assert detail["missing"] == ["observation.manage_any"]

    denied_disappeared = await client.post(DISAPPEARED.format(observation_id=observation.id), json={"disappeared_at": datetime.now(timezone.utc).isoformat()}, headers=headers(colleague))
    assert denied_disappeared.status_code == 403
    assert denied_disappeared.json()["detail"]["code"] == "not_owner"

    await grant(db, colleague, "observation.manage_any")
    allowed = await client.put(url, json={"comment": "managed"}, headers=headers(colleague))
    assert allowed.status_code == 200, allowed.text


@pytest.mark.asyncio
async def test_media_delete_ownership(client, db, headers):
    post = await make_post(db)
    ids = await make_dictionary(db)
    uploader = await make_user(db, perms=OPERATOR, post_id=post.id)
    colleague = await make_user(db, perms=OPERATOR, post_id=post.id)
    observation = await make_observation(db, uploader, post, ids["signal_type_id"])
    media = await make_media(db, observation, uploader)
    url = MediaRoutes.MEDIA_BY_ID.versioned_path().format(media_id=media.id)

    denied = await client.delete(url, headers=headers(colleague))
    assert denied.status_code == 403
    assert denied.json()["detail"]["code"] == "not_owner"
    assert denied.json()["detail"]["required"]["all"] == ["media.manage_any"]

    observation.status = "disappeared"
    db.add(observation)
    await db.commit()
    inactive = await client.delete(url, headers=headers(uploader))
    assert inactive.status_code == 403
    assert inactive.json()["detail"]["code"] == "observation_not_active"

    await grant(db, colleague, "media.manage_any")
    response = await client.delete(url, headers=headers(colleague))
    assert response.status_code not in (401, 403), response.text


@pytest.mark.asyncio
async def test_media_info_has_uploader(client, db, headers):
    post = await make_post(db)
    ids = await make_dictionary(db)
    uploader = await make_user(db, perms=OPERATOR, post_id=post.id)
    observation = await make_observation(db, uploader, post, ids["signal_type_id"])
    media = await make_media(db, observation, uploader)
    info = await client.get(MediaRoutes.MEDIA_INFO.versioned_path().format(media_id=media.id), headers=headers(uploader))
    assert info.status_code == 200, info.text
    user_info = info.json()["user"]
    assert {k: user_info[k] for k in ("id", "username", "username_alias")} == {"id": uploader.id, "username": uploader.username, "username_alias": uploader.username_alias}
    assert set(user_info) == {"id", "username", "username_alias", "is_online", "last_activity_at", "is_deleted"}
