"""Dictionary archive rules: create never restores, a category with signal types is not archived, a signal type of
an archived category is not restored, an observation of an archived signal type is not restored, and a PUT that keeps
the observation's own signal type does not check it again."""
import pytest
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.enums.routes import CategoryRoutes, ObservationRoutes, SignalTypeRoutes, TagRoutes
from app.backend.models.observation import Category, SignalType, Tag
from tests.helpers import make_admin, make_dictionary, make_observation, make_post, make_user


@pytest.mark.asyncio
async def test_tag_create_with_archived_name_is_refused(client, db, headers):
    admin = await make_admin(db)
    ids = await make_dictionary(db)
    tag = (await db.exec(select(Tag).where(Tag.id == ids["tag_id"]))).one()
    name = tag.name
    assert (await client.post(V1.path(TagRoutes.TAG_ARCHIVE, tag_id=tag.id), headers=headers(admin))).status_code == 200

    response = await client.post(V1.path(TagRoutes.CREATE), json={"name": name}, headers=headers(admin))
    assert response.json()["code"] == "tag_name_archived", response.text
    await db.refresh(tag)
    assert tag.is_deleted is True

    restored = await client.post(V1.path(TagRoutes.TAG_RESTORE, tag_id=tag.id), headers=headers(admin))
    assert restored.status_code == 200


@pytest.mark.asyncio
async def test_category_create_with_archived_name_is_refused(client, db, headers):
    admin = await make_admin(db)
    category = Category(name="Old category", alias="old")
    db.add(category)
    await db.commit()
    assert (await client.post(V1.path(CategoryRoutes.CATEGORY_ARCHIVE, category_id=category.id),
                              headers=headers(admin))).status_code == 200

    response = await client.post(V1.path(CategoryRoutes.CREATE), json={"name": "old category", "alias": "new"},
                                 headers=headers(admin))
    assert response.json()["code"] == "category_name_archived", response.text
    await db.refresh(category)
    assert category.is_deleted is True
    assert category.alias == "old"


@pytest.mark.asyncio
async def test_signal_type_create_with_archived_code_is_refused(client, db, headers):
    admin = await make_admin(db)
    ids = await make_dictionary(db)
    signal_type = (await db.exec(select(SignalType).where(SignalType.id == ids["signal_type_id"]))).one()
    code = signal_type.code
    assert (await client.post(V1.path(SignalTypeRoutes.SIGNAL_TYPE_ARCHIVE, signal_type_id=signal_type.id),
                              headers=headers(admin))).status_code == 200

    response = await client.post(
        V1.path(SignalTypeRoutes.CREATE), json={"code": code, "name": "Again"}, headers=headers(admin))
    assert response.json()["code"] == "signal_type_code_archived", response.text
    await db.refresh(signal_type)
    assert signal_type.is_deleted is True
    assert signal_type.category_id == ids["category_id"]


@pytest.mark.asyncio
async def test_category_with_signal_types_is_not_archived(client, db, headers):
    admin = await make_admin(db)
    ids = await make_dictionary(db)
    url = V1.path(CategoryRoutes.CATEGORY_ARCHIVE, category_id=ids["category_id"])

    response = await client.post(url, headers=headers(admin))
    assert response.status_code == 409 and response.json()["code"] == "category_has_signal_types", response.text

    assert (await client.post(V1.path(SignalTypeRoutes.SIGNAL_TYPE_ARCHIVE, signal_type_id=ids["signal_type_id"]),
                              headers=headers(admin))).status_code == 200
    assert (await client.post(url, headers=headers(admin))).status_code == 200


@pytest.mark.asyncio
async def test_signal_type_of_an_archived_category_is_not_restored(client, db, headers):
    admin = await make_admin(db)
    ids = await make_dictionary(db)
    assert (await client.post(V1.path(SignalTypeRoutes.SIGNAL_TYPE_ARCHIVE, signal_type_id=ids["signal_type_id"]),
                              headers=headers(admin))).status_code == 200
    assert (await client.post(V1.path(CategoryRoutes.CATEGORY_ARCHIVE, category_id=ids["category_id"]),
                              headers=headers(admin))).status_code == 200

    url = V1.path(SignalTypeRoutes.SIGNAL_TYPE_RESTORE, signal_type_id=ids["signal_type_id"])
    response = await client.post(url, headers=headers(admin))
    assert response.status_code == 409 and response.json()["code"] == "signal_type_category_archived", response.text

    assert (await client.post(V1.path(CategoryRoutes.CATEGORY_RESTORE, category_id=ids["category_id"]),
                              headers=headers(admin))).status_code == 200
    assert (await client.post(url, headers=headers(admin))).status_code == 200


@pytest.mark.asyncio
async def test_observation_of_an_archived_signal_type_is_not_restored(client, db, headers):
    post = await make_post(db)
    admin = await make_admin(db)
    ids = await make_dictionary(db)
    user = await make_user(db, perms=["observation.view", "observation.archive"], post_id=post.id)
    observation = await make_observation(db, user, post, ids["signal_type_id"])
    archive = V1.path(ObservationRoutes.OBSERVATION_ARCHIVE, observation_id=observation.id)
    restore = V1.path(ObservationRoutes.OBSERVATION_RESTORE, observation_id=observation.id)
    assert (await client.post(archive, headers=headers(user))).status_code == 200
    assert (await client.post(V1.path(SignalTypeRoutes.SIGNAL_TYPE_ARCHIVE, signal_type_id=ids["signal_type_id"]),
                              headers=headers(admin))).status_code == 200

    response = await client.post(restore, headers=headers(user))
    assert response.status_code == 409 and response.json()["code"] == "observation_signal_type_archived", response.text

    assert (await client.post(V1.path(SignalTypeRoutes.SIGNAL_TYPE_RESTORE, signal_type_id=ids["signal_type_id"]),
                              headers=headers(admin))).status_code == 200
    assert (await client.post(restore, headers=headers(user))).status_code == 200


def test_only_restore_declares_observation_signal_type_archived():
    """Archive never checks the signal type, so its operation does not list the code"""
    from app.main import fastapi_app
    from app.backend.enums.errors import ErrorCode
    from app.backend.errors.openapi import schema_name
    from app.backend.errors.problems import PROBLEM_JSON
    paths = V1.openapi(fastapi_app)["paths"]
    name = schema_name(ErrorCode.OBSERVATION_SIGNAL_TYPE_ARCHIVED)

    def declares(route):
        operation = paths[V1.path(route)]["post"]
        conflict = operation["responses"].get("409", {"content": {PROBLEM_JSON: {"schema": {"oneOf": []}}}})
        media = conflict["content"][PROBLEM_JSON]["schema"]
        return name in {ref["$ref"].rsplit("/", 1)[1] for ref in media.get("oneOf", [media])}

    assert declares(ObservationRoutes.OBSERVATION_RESTORE)
    assert not declares(ObservationRoutes.OBSERVATION_ARCHIVE)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [{"is_active": False}, {"is_deleted": True}])
async def test_put_with_the_own_signal_type_skips_its_checks(client, db, headers, change):
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, perms=["observation.view", "observation.update"], post_id=post.id)
    observation = await make_observation(db, user, post, ids["signal_type_id"])
    signal_type = (await db.exec(select(SignalType).where(SignalType.id == ids["signal_type_id"]))).one()
    for field, value in change.items():
        setattr(signal_type, field, value)
    db.add(signal_type)
    await db.commit()

    url = V1.path(ObservationRoutes.OBSERVATION_BY_ID, observation_id=observation.id)
    response = await client.put(
        url, json={"signal_type_id": ids["signal_type_id"], "description": "kept"}, headers=headers(user))
    assert response.status_code == 200, response.text

    other = await make_dictionary(db)
    other_type = (await db.exec(select(SignalType).where(SignalType.id == other["signal_type_id"]))).one()
    other_type.is_active = False
    db.add(other_type)
    await db.commit()
    refused = await client.put(url, json={"signal_type_id": other["signal_type_id"]}, headers=headers(user))
    assert refused.json()["code"] == "signal_type_inactive", refused.text
