"""Two requests pass the same uniqueness SELECT and the second one hits the unique index: the answer is the entity's
taken / exists code (or, for the idempotent PUTs, the stored row), never a 500, and create_user is one commit.

The race is made deterministic: the conflicting row is committed first and the pre-check SELECT is blinded (it
answers "no row"), so the request goes on to the INSERT the index refuses."""
from contextlib import contextmanager

import pytest
from sqlalchemy import event, false
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlmodel import select

from app.backend.api.shared.helpers.db_errors import unique_violation_as
from app.backend.api.v1 import V1
from app.backend.api.v1 import users as users_module
from app.backend.enums.routes import CategoryRoutes, MediaFormatRoutes, RoleRoutes, SignalTypeRoutes, TagRoutes
from app.backend.models.media_format import MediaFormat
from app.backend.models.observation import Category, SignalType, Tag
from app.backend.models.role import Permission, Role, RolePermission
from app.backend.models.user import User
from tests.helpers import make_admin, make_role, make_user
from tests.test_username_validation import create_user


@contextmanager
def blind_select(*fragments):
    """The first SELECT whose SQL holds every fragment (none of the "!"-prefixed ones) finds nothing (the other
    request had not committed yet)"""
    fired = []

    def hook(state):
        if fired or not state.is_select:
            return None
        sql = str(state.statement).lower()
        if all((fragment[1:] not in sql) if fragment.startswith("!") else (fragment in sql) for fragment in fragments):
            fired.append(sql)
            return state.invoke_statement(statement=state.statement.where(false()))
        return None

    event.listen(Session, "do_orm_execute", hook)
    try:
        yield fired
    finally:
        event.remove(Session, "do_orm_execute", hook)


# ----------------------------------------------------------------- dictionaries

@pytest.mark.asyncio
async def test_tag_create_race_answers_taken(client, db, headers):
    admin = await make_admin(db)
    db.add(Tag(name="racing"))
    await db.commit()
    with blind_select("lower(tags.name)") as fired:
        response = await client.post(V1.path(TagRoutes.CREATE), json={"name": "Racing"}, headers=headers(admin))
    assert fired, "the pre-check was blinded"
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "tag_name_taken"


@pytest.mark.asyncio
async def test_tag_rename_race_answers_taken(client, db, headers):
    admin = await make_admin(db)
    first, second = Tag(name="first"), Tag(name="second")
    db.add_all([first, second])
    await db.commit()
    with blind_select("lower(tags.name)", "tags.id !=") as fired:
        response = await client.put(V1.path(TagRoutes.TAG_BY_ID, tag_id=second.id), json={"name": "first"},
                                    headers=headers(admin))
    assert fired
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "tag_name_taken"


@pytest.mark.asyncio
async def test_category_create_race_answers_taken(client, db, headers):
    admin = await make_admin(db)
    db.add(Category(name="Racing", alias="r"))
    await db.commit()
    with blind_select("lower(categories.name)") as fired:
        response = await client.post(V1.path(CategoryRoutes.CREATE), json={"name": "Racing", "alias": "r2"},
                                     headers=headers(admin))
    assert fired
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "category_name_taken"


@pytest.mark.asyncio
async def test_category_rename_race_answers_taken(client, db, headers):
    admin = await make_admin(db)
    first, second = Category(name="First", alias="a"), Category(name="Second", alias="b")
    db.add_all([first, second])
    await db.commit()
    with blind_select("lower(categories.name)", "categories.id !=") as fired:
        response = await client.put(V1.path(CategoryRoutes.CATEGORY_BY_ID, category_id=second.id),
                                    json={"name": "First"}, headers=headers(admin))
    assert fired
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "category_name_taken"


@pytest.mark.asyncio
async def test_signal_type_create_race_answers_taken(client, db, headers):
    admin = await make_admin(db)
    db.add(SignalType(code="RACE", name="Racing"))
    await db.commit()
    with blind_select("lower(signal_types.code)") as fired:
        response = await client.post(V1.path(SignalTypeRoutes.CREATE), json={"code": "RACE", "name": "Other"},
                                     headers=headers(admin))
    assert fired
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "signal_type_code_taken"


@pytest.mark.asyncio
async def test_signal_type_recode_race_answers_taken(client, db, headers):
    admin = await make_admin(db)
    first, second = SignalType(code="FIRST", name="One"), SignalType(code="SECOND", name="Two")
    db.add_all([first, second])
    await db.commit()
    with blind_select("lower(signal_types.code)", "signal_types.id !=") as fired:
        response = await client.put(V1.path(SignalTypeRoutes.SIGNAL_TYPE_BY_ID, signal_type_id=second.id),
                                    json={"code": "FIRST"}, headers=headers(admin))
    assert fired
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "signal_type_code_taken"


# ----------------------------------------------------------------- media formats

MEDIA_FORMAT = {"media_type": "image", "label": "Race", "content_type": "image/x-race", "extensions": [".race"], "aliases": [],
                "max_file_size_bytes": 1024}


@pytest.mark.asyncio
async def test_media_format_create_race_answers_exists(client, db, headers):
    admin = await make_admin(db)
    other = MediaFormat(**{**MEDIA_FORMAT, "extensions": [".other"]})
    db.add(other)
    await db.commit()
    with blind_select("media_formats.content_type =") as fired, blind_select("media_formats.is_deleted =", "!media_formats.id =") as clashes:
        response = await client.post(V1.path(MediaFormatRoutes.CREATE), json=MEDIA_FORMAT, headers=headers(admin))
    assert fired and clashes
    assert response.status_code == 400, response.text
    body = response.json()
    assert body["code"] == "media_format_exists"
    assert body["content_type"] == "image/x-race" and body["format_id"] == other.id


@pytest.mark.asyncio
async def test_media_format_content_type_change_race_answers_exists(client, db, headers):
    admin = await make_admin(db)
    taken = MediaFormat(**{**MEDIA_FORMAT, "extensions": [".taken"]})
    mine = MediaFormat(**{**MEDIA_FORMAT, "content_type": "image/x-mine", "extensions": [".mine"]})
    db.add_all([taken, mine])
    await db.commit()
    with blind_select("media_formats.content_type =") as fired, blind_select("media_formats.is_deleted =", "!media_formats.id =") as clashes:
        response = await client.put(V1.path(MediaFormatRoutes.FORMAT_BY_ID, format_id=mine.id),
                                    json={"content_type": "image/x-race"}, headers=headers(admin))
    assert fired and clashes
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "media_format_exists"


# ----------------------------------------------------------------- roles

@pytest.mark.asyncio
async def test_role_create_race_answers_taken(client, db, headers):
    admin = await make_admin(db)
    await make_role(db, [], name="racing")
    with blind_select("roles.name =") as fired:
        response = await client.post(V1.path(RoleRoutes.ROOT), headers=headers(admin),
                                     json={"name": "racing", "display_name": "Racing", "kind": "tree"})
    assert fired
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "role_name_taken"
    assert len((await db.exec(select(Role).where(Role.name == "racing"))).all()) == 1


@pytest.mark.asyncio
async def test_role_rename_race_answers_taken(client, db, headers):
    admin = await make_admin(db)
    await make_role(db, [], name="first")
    second = await make_role(db, [], name="second")
    with blind_select("roles.name =", "roles.id !=") as fired:
        response = await client.patch(V1.path(RoleRoutes.ROLE_BY_ID, role_id=second.id), headers=headers(admin),
                                      json={"name": "first"})
    assert fired
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "role_name_taken"


@pytest.mark.asyncio
async def test_role_permission_put_race_is_idempotent(client, db, headers):
    admin = await make_admin(db)
    role = await make_role(db, ["tag.view"])
    permission = (await db.exec(select(Permission).where(Permission.name == "tag.view"))).one()
    with blind_select("from role_permissions", "role_permissions.permission_id = :") as fired:
        response = await client.put(
            V1.path(RoleRoutes.ROLE_PERMISSION_BY_ID, role_id=role.id, permission_id=permission.id),
            headers=headers(admin))
    assert fired
    assert response.status_code == 200, response.text
    assert [item["name"] for item in response.json()] == ["tag.view"]
    rows = (await db.exec(select(RolePermission).where(RolePermission.role_id == role.id))).all()
    assert len(rows) == 1


# ----------------------------------------------------------------- users

@pytest.mark.asyncio
async def test_user_create_race_answers_taken_and_leaves_one_user(client, db, headers):
    admin = await make_admin(db)
    await make_user(db, username="racing")
    with blind_select("lower(users.username)") as fired:
        response = await create_user(client, headers(admin), "racing")
    assert fired
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "username_or_certificate_taken"
    names = (await db.exec(select(User.username).where(User.username == "racing"))).all()
    assert names == ["racing"]


@pytest.mark.asyncio
async def test_user_create_writes_user_and_assignment_in_one_commit(client, db, headers, monkeypatch):
    admin = await make_admin(db)

    def refuse(**fields):
        raise RuntimeError("assignment row refused")

    monkeypatch.setattr(users_module, "RoleAssignment", refuse)
    response = await create_user(client, headers(admin), "halfmade")
    assert response.status_code == 500
    assert (await db.exec(select(User).where(User.username == "halfmade"))).first() is None


# ----------------------------------------------------------------- helper

@pytest.mark.asyncio
async def test_other_integrity_errors_are_not_mapped(db):
    """Only a unique violation becomes the entity's code; a NOT NULL violation stays what it is"""
    db.add(Tag(name=None))
    with pytest.raises(IntegrityError):
        async with unique_violation_as(db, "never_used"):
            await db.commit()
