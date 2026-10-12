"""Ownership: observation.manage_any and media.manage_any."""
from datetime import datetime, timezone

import pytest
from sqlmodel import select

from app.backend.enums.routes import ObservationRoutes, MediaRoutes
from app.backend.api.v1 import V1
from app.backend.models.role import Permission, RolePermission
from app.backend.models.role_assignment import RoleAssignment
from tests.helpers import make_post, make_user, make_dictionary, make_observation, make_media

UPDATE = V1.path(ObservationRoutes.OBSERVATION_BY_ID)
DISAPPEARED = V1.path(ObservationRoutes.MARK_DISAPPEARED)
OPERATOR = ["observation.view", "observation.update", "media.view", "media.archive"]


async def grant(db, user, permission_name: str):
    role_id = (await db.exec(select(RoleAssignment.role_id).where(RoleAssignment.user_id == user.id))).first()
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
    detail = denied.json()
    assert detail["code"] == "observation_not_owner"
    assert detail["required"]["all"] == ["observation.manage_any"]
    assert detail["missing"] == ["observation.manage_any"]

    denied_disappeared = await client.post(DISAPPEARED.format(observation_id=observation.id), json={"disappeared_at": datetime.now(timezone.utc).isoformat()}, headers=headers(colleague))
    assert denied_disappeared.status_code == 403
    assert denied_disappeared.json()["code"] == "observation_not_owner"

    await grant(db, colleague, "observation.manage_any")
    allowed = await client.put(url, json={"comment": "managed"}, headers=headers(colleague))
    assert allowed.status_code == 200, allowed.text


@pytest.mark.asyncio
async def test_media_archive_ownership(client, db, headers):
    post = await make_post(db)
    ids = await make_dictionary(db)
    uploader = await make_user(db, perms=OPERATOR, post_id=post.id)
    colleague = await make_user(db, perms=OPERATOR, post_id=post.id)
    observation = await make_observation(db, uploader, post, ids["signal_type_id"])
    media = await make_media(db, observation, uploader)
    url = V1.path(MediaRoutes.MEDIA_ARCHIVE, media_id=media.id)

    denied = await client.post(url, headers=headers(colleague))
    assert denied.status_code == 403
    assert denied.json()["code"] == "media_not_owner"
    assert denied.json()["required"]["all"] == ["media.manage_any"]

    observation.status = "disappeared"
    db.add(observation)
    await db.commit()
    inactive = await client.post(url, headers=headers(uploader))
    assert inactive.status_code == 403
    assert inactive.json()["code"] == "observation_not_active"

    await grant(db, colleague, "media.manage_any")
    response = await client.post(url, headers=headers(colleague))
    assert response.status_code == 200, response.text
    assert response.json()["is_deleted"] is True and response.json()["observation_id"] == observation.id

    # archived media is hidden from the observation, restore brings it back
    listing = V1.path(ObservationRoutes.LIST_ALL)

    async def media_ids():
        rows = (await client.get(listing, headers=headers(uploader))).json()
        return next(o for o in rows if o["id"] == observation.id)["media_ids"]

    assert await media_ids() == []
    assert (await client.post(url, headers=headers(colleague))).status_code == 404
    restore = V1.path(MediaRoutes.MEDIA_RESTORE, media_id=media.id)
    assert (await client.post(restore, headers=headers(uploader))).status_code == 403, "observation is not active"
    restored = await client.post(restore, headers=headers(colleague))
    assert restored.status_code == 200 and restored.json()["is_deleted"] is False
    assert await media_ids() == [media.id]
    assert (await client.post(restore, headers=headers(colleague))).status_code == 409


@pytest.mark.asyncio
async def test_media_info_has_uploader(client, db, headers):
    post = await make_post(db)
    ids = await make_dictionary(db)
    uploader = await make_user(db, perms=OPERATOR, post_id=post.id)
    observation = await make_observation(db, uploader, post, ids["signal_type_id"])
    media = await make_media(db, observation, uploader)
    info = await client.get(V1.path(MediaRoutes.MEDIA_INFO, media_id=media.id), headers=headers(uploader))
    assert info.status_code == 200, info.text
    user_info = info.json()["user"]
    assert {k: user_info[k] for k in ("id", "username", "username_alias")} == {"id": uploader.id, "username": uploader.username, "username_alias": uploader.username_alias}
    assert set(user_info) == {"id", "username", "username_alias", "is_online", "last_activity_at", "is_deleted"}
