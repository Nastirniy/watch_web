"""Check order: scope and ownership answer before the state of a row or the existence of a post, so an outsider
learns nothing about ids. User visibility follows the list, archived users stay hidden."""
import pytest

from app.backend.api.v1 import V1
from app.backend.auth.scope import Node
from app.backend.enums.routes import MediaRoutes, ObservationRoutes, UserRoutes
from tests.helpers import (
    ACCESS_ALL, ADMIN_TRUE, assign, make_admin, make_post, make_role, make_user, make_dictionary, make_observation,
    make_media, observation_payload,
)
from tests.users.test_username_validation import make_certificate

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
    user = await make_user(db, perms=["observation.view", "observation.create", ACCESS_ALL])
    response = await client.post(V1.path(ObservationRoutes.CREATE), json=observation_payload(999999, ids["signal_type_id"]), headers=headers(user))
    assert response.status_code == 404
    assert response.json()["code"] == "post_not_found"


@pytest.mark.fit_types
@pytest.mark.asyncio
async def test_user_create_and_move_answer_alike_for_unknown_archived_and_foreign_posts(client, db, headers):
    """A caller holding user.create and user.update at one post learns nothing about other post ids: an unknown id,
    an archived post and a live post outside the scope get the same 403 on POST /users/ and on a move; 404 and
    409 answer only inside the scope"""
    home, archived, foreign = await make_post(db), await make_post(db), await make_post(db)
    archived.is_deleted = True
    db.add(archived)
    await db.commit()
    caller = await make_user(db)
    await assign(db, caller, await make_role(db, ["user.view", "user.create", "user.update"]), Node.post(home.id))
    target = await make_user(db, post_id=home.id)
    admin = await make_admin(db)
    outside = (999999, archived.id, foreign.id)

    async def create(who, post_id: int):
        name = f"new_{who.id}_{post_id}"
        return await client.post(
            V1.path(UserRoutes.ROOT), headers=headers(who),
            files={"certificate": ("cert.pem", make_certificate(f"cn-{name}"), "application/x-pem-file")},
            data={"username": name, "username_alias": "New", "user_type": "viewer", "post_id": str(post_id)},
        )

    async def move(who, post_id: int):
        return await client.patch(V1.path(UserRoutes.USER_BY_ID, user_id=target.id), headers=headers(who),
                                  data={"post_id": str(post_id)})

    for call, action, permission in ((create, "create_user", "user.create"), (move, "move_user", "user.update")):
        answers = [await call(caller, post_id) for post_id in outside]
        for post_id, response in zip(outside, answers):
            assert response.status_code == 403, (action, post_id, response.text)
        bodies = [{key: response.json()[key] for key in ("code", "permission", "action", "missing")} for response in answers]
        assert bodies == [{"code": "permission_required_for_action", "permission": permission, "action": action,
                           "missing": [permission]}] * len(outside), bodies

        # the state of a post answers only where the caller's scope reaches it (the root here)
        unknown, gone = await call(admin, 999999), await call(admin, archived.id)
        assert (unknown.status_code, unknown.json()["code"]) == (404, "post_not_found"), unknown.text
        assert (gone.status_code, gone.json()["code"]) == (409, "post_archived"), gone.text


@pytest.mark.asyncio
async def test_access_all_opens_a_user_without_a_post(client, db, headers):
    viewer = await make_user(db, perms=["user.view", ACCESS_ALL])
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

    plain = await make_user(db, perms=["user.view", ACCESS_ALL])
    archiver = await make_user(db, perms=["user.view", "user.archive", ADMIN_TRUE])
    # admin.true alone no longer shows archived records; user.archive without admin.true is held nowhere
    admin = await make_user(db, perms=["user.view", ADMIN_TRUE])
    dead_archive = await make_user(db, perms=["user.view", "user.archive", ACCESS_ALL])
    for user in (plain, admin, dead_archive):
        assert (await client.get(url, headers=headers(user))).status_code == 404
    assert (await client.get(url, headers=headers(archiver))).status_code == 200


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


@pytest.mark.fit_types
@pytest.mark.asyncio
async def test_user_update_outsider_gets_the_same_403_for_an_archived_and_a_live_user(client, db, headers):
    """PATCH /users/{user_id}: the caller's reach answers before the archived state (404 only inside it)"""
    home, other = await make_post(db), await make_post(db)
    live = await make_user(db, perms=["observation.view"], post_id=home.id)
    archived = await make_user(db, perms=["observation.view"], post_id=home.id)
    archived.is_deleted = True
    db.add(archived)
    await db.commit()
    outsider = await make_user(db, post_id=other.id)
    await assign(db, outsider, await make_role(db, ["user.view", "user.update"]), Node.post(other.id))

    for target in (live, archived):
        response = await client.patch(V1.path(UserRoutes.USER_BY_ID, user_id=target.id),
                                      data={"username_alias": "X"}, headers=headers(outsider))
        assert response.status_code == 403, (target.is_deleted, response.text)
        # The user lies outside the caller's user.view: the 403 of GET /users/{user_id} (audit L1)
        assert response.json()["code"] == "user_self_only"
    admin = await make_admin(db)
    hidden = await client.patch(V1.path(UserRoutes.USER_BY_ID, user_id=archived.id), data={"username_alias": "X"},
                                headers=headers(admin))
    assert hidden.status_code == 404 and hidden.json()["code"] == "user_not_found"


@pytest.mark.asyncio
@pytest.mark.parametrize("route, permission", [
    (UserRoutes.USER_ARCHIVE, "user.archive"),
    (UserRoutes.USER_RESTORE, "user.archive"),
    (UserRoutes.USER_SESSIONS_END, "user.update"),
    (UserRoutes.USER_SESSION_END, "user.update"),
])
@pytest.mark.fit_types
async def test_user_state_routes_answer_the_outsider_alike(client, db, headers, route, permission):
    """R39: archive, restore and ending sessions check the caller's reach before the archived state of the user (404
    user_not_found, 409 user_not_archived only inside it), so an outsider gets the same 403 for an archived and a live
    user"""
    home, other = await make_post(db), await make_post(db)
    live = await make_user(db, perms=["observation.view"], post_id=home.id)
    archived = await make_user(db, perms=["observation.view"], post_id=home.id)
    archived.is_deleted = True
    db.add(archived)
    await db.commit()
    outsider = await make_user(db, post_id=other.id)
    await assign(db, outsider, await make_role(db, ["user.view", permission]), Node.post(other.id))

    def url(target) -> str:
        params = {"session_id": "no-such-session"} if route is UserRoutes.USER_SESSION_END else {}
        return V1.path(route, user_id=target.id, **params)

    for target in (live, archived):
        response = await client.post(url(target), headers=headers(outsider))
        assert response.status_code == 403, (target.is_deleted, response.text)
        # The user lies outside the caller's user.view: the 403 of GET /users/{user_id} (audit L1)
        assert response.json()["code"] == "user_self_only", response.text

    # Inside the caller's reach the state answers
    admin = await make_admin(db)
    state = {UserRoutes.USER_RESTORE: (live, 409, "user_not_archived")}.get(route, (archived, 404, "user_not_found"))
    response = await client.post(url(state[0]), headers=headers(admin))
    assert (response.status_code, response.json()["code"]) == state[1:], response.text
