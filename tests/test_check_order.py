"""Check order: scope and ownership answer before the state of a row or the existence of a post, so an outsider
learns nothing about ids. User visibility follows the list, archived users stay hidden, a home post move drops the
now redundant access row."""
import pytest
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.enums.routes import MediaRoutes, ObservationRoutes, UserRoutes
from app.backend.models.audit import AuditLog
from app.backend.models.user_post_access import UserPostAccess
from tests.helpers import make_post, make_user, make_admin, make_dictionary, make_observation, make_media, observation_payload

MEDIA_PERMS = ["media.view", "media.archive"]
OBS_PERMS = ["observation.view", "observation.archive"]


@pytest.mark.asyncio
@pytest.mark.parametrize("route", [MediaRoutes.MEDIA_ARCHIVE, MediaRoutes.MEDIA_RESTORE])
@pytest.mark.parametrize("archived", [False, True])
async def test_media_outsider_gets_the_same_403_whatever_the_state(client, db, headers, route, archived):
    home, other = await make_post(db), await make_post(db)
    ids = await make_dictionary(db)
    owner = await make_user(db, perms=MEDIA_PERMS, post_id=home.id)
    outsider = await make_user(db, perms=MEDIA_PERMS, post_id=other.id)
    observation = await make_observation(db, owner, home, ids["signal_type_id"])
    media = await make_media(db, observation, owner, is_deleted=archived)

    response = await client.post(V1.path(route, media_id=media.id), headers=headers(outsider))
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "post_access_denied"


@pytest.mark.asyncio
async def test_media_state_answers_for_a_caller_in_scope(client, db, headers):
    post = await make_post(db)
    ids = await make_dictionary(db)
    owner = await make_user(db, perms=MEDIA_PERMS, post_id=post.id)
    observation = await make_observation(db, owner, post, ids["signal_type_id"])
    live = await make_media(db, observation, owner)
    gone = await make_media(db, observation, owner, is_deleted=True)

    assert (await client.post(V1.path(MediaRoutes.MEDIA_RESTORE, media_id=live.id), headers=headers(owner))).status_code == 409
    assert (await client.post(V1.path(MediaRoutes.MEDIA_ARCHIVE, media_id=gone.id), headers=headers(owner))).status_code == 404
    assert (await client.post(V1.path(MediaRoutes.MEDIA_ARCHIVE, media_id=999999), headers=headers(owner))).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("route", [ObservationRoutes.OBSERVATION_ARCHIVE, ObservationRoutes.OBSERVATION_RESTORE])
@pytest.mark.parametrize("archived", [False, True])
async def test_observation_outsider_gets_the_same_403_whatever_the_state(client, db, headers, route, archived):
    home, other = await make_post(db), await make_post(db)
    ids = await make_dictionary(db)
    owner = await make_user(db, perms=OBS_PERMS, post_id=home.id)
    outsider = await make_user(db, perms=OBS_PERMS, post_id=other.id)
    observation = await make_observation(db, owner, home, ids["signal_type_id"])
    observation.is_deleted = archived
    db.add(observation)
    await db.commit()

    response = await client.post(V1.path(route, observation_id=observation.id), headers=headers(outsider))
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "post_access_denied"


@pytest.mark.asyncio
async def test_observation_state_answers_for_the_owner(client, db, headers):
    post = await make_post(db)
    ids = await make_dictionary(db)
    owner = await make_user(db, perms=OBS_PERMS, post_id=post.id)
    observation = await make_observation(db, owner, post, ids["signal_type_id"])

    assert (await client.post(V1.path(ObservationRoutes.OBSERVATION_RESTORE, observation_id=observation.id), headers=headers(owner))).status_code == 409
    assert (await client.post(V1.path(ObservationRoutes.OBSERVATION_ARCHIVE, observation_id=observation.id), headers=headers(owner))).status_code == 200
    assert (await client.post(V1.path(ObservationRoutes.OBSERVATION_ARCHIVE, observation_id=observation.id), headers=headers(owner))).status_code == 404


@pytest.mark.asyncio
async def test_create_observation_unknown_and_foreign_post_answer_alike(client, db, headers):
    home, foreign = await make_post(db), await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, perms=["observation.view", "observation.create"], post_id=home.id)
    create = V1.path(ObservationRoutes.CREATE)

    unknown = await client.post(create, json=observation_payload(999999, ids["signal_type_id"]), headers=headers(user))
    real = await client.post(create, json=observation_payload(foreign.id, ids["signal_type_id"]), headers=headers(user))
    assert unknown.status_code == real.status_code == 403
    assert unknown.json()["code"] == real.json()["code"] == "post_access_denied"


@pytest.mark.asyncio
async def test_create_observation_unknown_post_is_404_for_access_all(client, db, headers):
    ids = await make_dictionary(db)
    user = await make_user(db, perms=["observation.view", "observation.create", "post.access_all"])
    response = await client.post(V1.path(ObservationRoutes.CREATE), json=observation_payload(999999, ids["signal_type_id"]), headers=headers(user))
    assert response.status_code == 404
    assert response.json()["code"] == "post_not_found"


@pytest.mark.asyncio
async def test_access_all_opens_a_user_without_a_post(client, db, headers):
    viewer = await make_user(db, perms=["user.view", "post.access_all"])
    target = await make_user(db)
    assert target.post_id is None

    listed = await client.get(V1.path(UserRoutes.ROOT), headers=headers(viewer))
    assert target.id in {item["id"] for item in listed.json()}
    response = await client.get(V1.path(UserRoutes.USER_BY_ID, user_id=target.id), headers=headers(viewer))
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_archived_user_is_hidden_without_user_archive(client, db, headers):
    post = await make_post(db)
    target = await make_user(db, post_id=post.id)
    target.is_deleted = True
    db.add(target)
    await db.commit()
    url = V1.path(UserRoutes.USER_BY_ID, user_id=target.id)

    plain = await make_user(db, perms=["user.view", "post.access_all"])
    archiver = await make_user(db, perms=["user.view", "user.archive", "post.access_all"])
    admin = await make_user(db, perms=["user.view", "admin.true"])
    assert (await client.get(url, headers=headers(plain))).status_code == 404
    assert (await client.get(url, headers=headers(archiver))).status_code == 200
    assert (await client.get(url, headers=headers(admin))).status_code == 200


@pytest.mark.asyncio
async def test_user_outsider_gets_the_same_403_for_an_archived_and_a_live_user(client, db, headers):
    home, other = await make_post(db), await make_post(db)
    live = await make_user(db, post_id=home.id)
    archived = await make_user(db, post_id=home.id)
    archived.is_deleted = True
    db.add(archived)
    await db.commit()
    outsider = await make_user(db, perms=["user.view"], post_id=other.id)

    for target in (live, archived):
        response = await client.get(V1.path(UserRoutes.USER_BY_ID, user_id=target.id), headers=headers(outsider))
        assert response.status_code == 403, (target.is_deleted, response.text)
        assert response.json()["code"] == "user_self_only"


@pytest.mark.asyncio
async def test_home_post_move_drops_the_access_row(client, db, headers):
    old_home, new_home = await make_post(db), await make_post(db)
    admin = await make_admin(db)
    target = await make_user(db, post_id=old_home.id, extra_access={new_home.id: True})
    target_id, new_home_id = target.id, new_home.id

    response = await client.patch(V1.path(UserRoutes.USER_BY_ID, user_id=target_id), data={"post_id": str(new_home_id)}, headers=headers(admin))
    assert response.status_code == 200, response.text
    assert response.json()["post_id"] == new_home_id

    db.expire_all()
    rows = (await db.exec(select(UserPostAccess).where(UserPostAccess.user_id == target_id))).all()
    assert rows == []
    audit = (await db.exec(
        select(AuditLog).where(AuditLog.resource_type == "user", AuditLog.resource_id == target_id, AuditLog.action == "update")
    )).all()
    assert audit and audit[-1].new_values["removed_post_access"] == [{"post_id": new_home_id, "can_write": True}]
