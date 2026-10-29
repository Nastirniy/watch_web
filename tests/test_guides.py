"""User guides (part 2): the tables, the system permissions guide.create / guide.update / guide.archive and the
audit events of guides and their files (task 4); the guide CRUD routes, the stored audience, archive and restore
(task 5). Files (task 6) and audience filtering (task 7) come later."""
from typing import get_type_hints

import pytest
from pydantic_core import PydanticCustomError
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.auth.builtin_roles import BUILTIN_ROLES
from app.backend.auth.context import load_auth_context
from app.backend.auth.scope import ROOT
from app.backend.auth.user_types import boundary
from app.backend.bootstrap.permissions import PERMISSION_META, permission_level
from app.backend.enums.audit import AuditCategory, AuditEvent, audit_event
from app.backend.enums.general import BuiltinRole, PermissionLevel, Permissions, RoleKind, UserType
from app.backend.enums.routes import GuideRoutes
from app.backend.models import UserGuide, UserGuideFile
from app.backend.models.audit import AuditLog
from app.backend.models.post import Post
from app.backend.models.role import Permission, Role, RolePermission
from app.backend.models.role_assignment import RoleAssignment, scope_columns
from app.backend.models.scope_group import ScopeGroup
from app.backend.models.system_role_assignment import SystemRoleAssignment
from app.backend.models.user import User
from tests.helpers import builtin_role, make_admin, make_group, make_post, make_user

GUIDE_PERMISSIONS = ("guide.create", "guide.update", "guide.archive")


# --- permissions -----------------------------------------------------------------------------------------------------

def test_guide_permission_names():
    assert (Permissions.GUIDE_CREATE.value, Permissions.GUIDE_UPDATE.value,
            Permissions.GUIDE_ARCHIVE.value) == GUIDE_PERMISSIONS


@pytest.mark.parametrize("name", GUIDE_PERMISSIONS)
def test_guide_permissions_are_system_level(name):
    assert permission_level(name) is PermissionLevel.SYSTEM
    display_name, description = PERMISSION_META[name]
    assert display_name and description


def test_only_the_system_administrator_role_and_type_hold_them():
    """EVERY_SYSTEM: the built-in system_administrator role and the system_administrator type; the administrator
    type's system boundary is not extended (controller ruling)"""
    assert set(GUIDE_PERMISSIONS) <= BUILTIN_ROLES[BuiltinRole.SYSTEM_ADMINISTRATOR].permissions
    assert set(GUIDE_PERMISSIONS) <= boundary(UserType.SYSTEM_ADMINISTRATOR, RoleKind.SYSTEM)
    for role, spec in BUILTIN_ROLES.items():
        if role is not BuiltinRole.SYSTEM_ADMINISTRATOR:
            assert not set(GUIDE_PERMISSIONS) & spec.permissions, role
    for user_type in UserType:
        if user_type is not UserType.SYSTEM_ADMINISTRATOR:
            assert not set(GUIDE_PERMISSIONS) & boundary(user_type, RoleKind.SYSTEM), user_type


async def test_seeding_writes_them_and_grants_them_to_system_administrator(db):
    levels = dict((await db.exec(select(Permission.name, Permission.level).where(
        Permission.name.in_(GUIDE_PERMISSIONS)))).all())
    assert levels == {name: "system" for name in GUIDE_PERMISSIONS}
    held = set((await db.exec(select(Permission.name).select_from(RolePermission).join(
        Permission, Permission.id == RolePermission.permission_id).join(
        Role, Role.id == RolePermission.role_id).where(
        Role.builtin_key == BuiltinRole.SYSTEM_ADMINISTRATOR.value))).all())
    assert set(GUIDE_PERMISSIONS) <= held


async def test_a_system_administrator_holds_them(db):
    admin = await make_admin(db)
    auth = await load_auth_context(db, admin)
    for name in GUIDE_PERMISSIONS:
        assert auth.has_at_root(name), name


async def test_a_plain_administrator_does_not_get_them(db):
    """The administrator type (tree): its base role administrator at the root holds no system permission, and even a
    system_administrator assignment puts none of guide.* in effect (the type's system boundary leaves them out)"""
    user = User(username="plain_admin", username_alias="Plain admin", user_type=UserType.ADMINISTRATOR.value)
    db.add(user)
    await db.flush()
    db.add(RoleAssignment(user_id=user.id, role_id=(await builtin_role(db, BuiltinRole.ADMINISTRATOR)).id,
                          **scope_columns(ROOT)))
    await db.commit()
    auth = await load_auth_context(db, user)
    assert not set(GUIDE_PERMISSIONS) & auth.held_anywhere

    db.add(SystemRoleAssignment(user_id=user.id,
                                role_id=(await builtin_role(db, BuiltinRole.SYSTEM_ADMINISTRATOR)).id))
    await db.commit()
    auth = await load_auth_context(db, user)
    assert not set(GUIDE_PERMISSIONS) & auth.held_anywhere


# --- audit events ----------------------------------------------------------------------------------------------------

GUIDE_EVENTS = {
    "user_guide.create": AuditEvent.USER_GUIDE_CREATE,
    "user_guide.update": AuditEvent.USER_GUIDE_UPDATE,
    "user_guide.archive": AuditEvent.USER_GUIDE_ARCHIVE,
    "user_guide.restore": AuditEvent.USER_GUIDE_RESTORE,
}
FILE_EVENTS = {
    "user_guide_file.upload": AuditEvent.USER_GUIDE_FILE_UPLOAD,
    "user_guide_file.archive": AuditEvent.USER_GUIDE_FILE_ARCHIVE,
    "user_guide_file.restore": AuditEvent.USER_GUIDE_FILE_RESTORE,
}


def test_guide_events_are_in_the_catalog():
    for code, member in {**GUIDE_EVENTS, **FILE_EVENTS}.items():
        assert audit_event(code) is member
        assert member.category is AuditCategory.DICTIONARIES, code
    for member in GUIDE_EVENTS.values():
        assert member.params == {"guide_title": str}, member.code
    for member in FILE_EVENTS.values():
        assert member.params == {"guide_title": str, "file_name": str, "size_bytes": int, "sha256": str}, member.code


# --- tables ----------------------------------------------------------------------------------------------------------

def test_guide_columns():
    guides = UserGuide.__table__
    assert set(guides.c.keys()) == {
        "id", "title", "description", "sort_order", "audience_key", "is_deleted", "created_at", "updated_at",
        "created_by", "updated_by"}
    assert (guides.c.title.type.length, guides.c.description.type.length, guides.c.audience_key.type.length) == (
        200, 2000, 24)
    assert not guides.c.title.nullable and guides.c.description.nullable and guides.c.audience_key.nullable
    assert not guides.c.created_by.foreign_keys and not guides.c.updated_by.foreign_keys

    files = UserGuideFile.__table__
    assert set(files.c.keys()) == {
        "id", "guide_id", "file_name", "stored_name", "file_path", "content_type", "format_id", "size_bytes",
        "sha256", "language", "is_deleted", "created_at", "created_by"}
    assert {fk.target_fullname for fk in files.c.guide_id.foreign_keys} == {"user_guides.id"}
    assert not files.c.created_by.foreign_keys and not files.c.format_id.foreign_keys
    assert (files.c.file_name.type.length, files.c.stored_name.type.length, files.c.file_path.type.length,
            files.c.sha256.type.length, files.c.language.type.length) == (255, 64, 500, 64, 8)
    assert files.c.size_bytes.type.__class__.__name__ == "BigInteger"
    assert files.c.language.nullable and not files.c.sha256.nullable


def test_guide_indexes():
    assert {(i.name, tuple(c.name for c in i.columns)) for i in UserGuide.__table__.indexes} == {
        ("ix_user_guides_list", ("is_deleted", "sort_order", "title"))}
    assert {(i.name, tuple(c.name for c in i.columns)) for i in UserGuideFile.__table__.indexes} == {
        ("ix_user_guide_files_guide_id", ("guide_id",))}


async def test_a_guide_and_a_file_round_trip(db):
    guide = UserGuide(title="Getting started", created_by=1, updated_by=1)
    db.add(guide)
    await db.flush()
    db.add(UserGuideFile(guide_id=guide.id, file_name="start.pdf", stored_name="ab" * 16 + ".pdf",
                         file_path=f"_guides/{guide.id}/1/start.pdf", content_type="application/pdf", format_id=1,
                         size_bytes=5_000_000_000, sha256="cd" * 32, created_by=1))
    await db.commit()
    stored = (await db.exec(select(UserGuideFile).where(UserGuideFile.guide_id == guide.id))).one()
    assert (stored.size_bytes, stored.is_deleted, stored.language) == (5_000_000_000, False, None)
    assert (guide.sort_order, guide.is_deleted, guide.audience_key, guide.description) == (0, False, None, None)
    assert get_type_hints(UserGuide)["sort_order"] is int


# --- routes (task 5) -------------------------------------------------------------------------------------------------

GUIDES = V1.path(GuideRoutes.ROOT)


def by_id(guide_id: int) -> str:
    return V1.path(GuideRoutes.GUIDE_BY_ID, guide_id=guide_id)


def archive_path(guide_id: int) -> str:
    return V1.path(GuideRoutes.GUIDE_ARCHIVE, guide_id=guide_id)


def restore_path(guide_id: int) -> str:
    return V1.path(GuideRoutes.GUIDE_RESTORE, guide_id=guide_id)


def collection(guide_id: int) -> str:
    return GUIDES


async def _operator(db) -> User:
    """A plain user: tree permissions at a home post, none of guide.*"""
    post = await make_post(db)
    return await make_user(db, perms=["observation.view", "observation.create"], post_id=post.id)


async def _create(client, headers, user, **body):
    response = await client.post(GUIDES, headers=headers(user), json={"title": "Getting started", **body})
    assert response.status_code == 201, response.text
    return response.json()


async def _audit_rows(db, guide_id: int):
    return (await db.exec(select(AuditLog).where(
        AuditLog.resource_type == "user_guide", AuditLog.resource_id == guide_id).order_by(AuditLog.id))).all()


async def test_create_returns_the_guide(client, db, headers):
    admin = await make_admin(db)
    body = await _create(client, headers, admin, description="How to log in\nand where to start", sort_order=3)
    assert body["title"] == "Getting started" and body["description"] == "How to log in\nand where to start"
    assert (body["sort_order"], body["audience"], body["files"], body["is_deleted"]) == (3, None, [], False)
    assert body["created_at"] and body["updated_at"]
    row = await db.get(UserGuide, body["id"])
    assert (row.created_by, row.updated_by, row.audience_key) == (admin.id, admin.id, None)


NATURAL_TITLE = "How to create, select, update and drop an observation"
NATURAL_DESCRIPTION = "Select the post, then create the observation.\nTo update it: open it.\tTo drop a band: archive it."


async def test_natural_text_with_sql_words_is_stored_and_returned_exactly(client, db, headers):
    """Guide text is plain text (SecurityValidators.plain_text): SQL words are harmless, values are bound parameters"""
    admin = await make_admin(db)
    body = await _create(client, headers, admin, title=NATURAL_TITLE, description=NATURAL_DESCRIPTION)
    assert (body["title"], body["description"]) == (NATURAL_TITLE, NATURAL_DESCRIPTION)
    row = await db.get(UserGuide, body["id"])
    assert (row.title, row.description) == (NATURAL_TITLE, NATURAL_DESCRIPTION)

    patched = await client.patch(by_id(body["id"]), headers=headers(admin),
                                 json={"title": "Drop or update: " + NATURAL_TITLE, "description": "SELECT * is fine"})
    assert patched.status_code == 200, patched.text
    assert (patched.json()["title"], patched.json()["description"]) == (
        "Drop or update: " + NATURAL_TITLE, "SELECT * is fine")
    assert (await client.get(by_id(body["id"]), headers=headers(admin))).json()["title"] == "Drop or update: " + NATURAL_TITLE


CONTROL = "text_control_character"


@pytest.mark.parametrize("method, body, pointer, code", [
    ("POST", {"title": "Bad\x07title"}, "#/title", CONTROL),
    ("POST", {"title": "Two\nlines"}, "#/title", CONTROL),
    ("POST", {"title": "ok", "description": "Bad\x00text"}, "#/description", CONTROL),
    # raw JSON with a lone surrogate escape: pydantic refuses it before the field validator
    ("POST", '{"title": "ok", "description": "Lone \\ud800 surrogate"}', "#/description", "string_unicode"),
    ("PATCH", {"title": "Bad\x1btitle"}, "#/title", CONTROL),
    ("PATCH", {"description": "Bad\x7ftext"}, "#/description", CONTROL),
])
async def test_control_characters_are_refused(client, db, headers, method, body, pointer, code):
    admin = await make_admin(db)
    created = await _create(client, headers, admin)
    path = GUIDES if method == "POST" else by_id(created["id"])
    if isinstance(body, str):   # a lone surrogate escape: httpx cannot encode it, so the JSON text is sent as it is
        response = await client.request(method, path, content=body.encode("ascii"),
                                        headers={**headers(admin), "Content-Type": "application/json"})
    else:
        response = await client.request(method, path, headers=headers(admin), json=body)
    assert response.status_code == 422, response.text
    items = [item for item in response.json()["errors"] if item.get("pointer") == pointer]
    assert [item["code"] for item in items] == [code], response.json()


@pytest.mark.parametrize("title", ["<script>alert(1)</script>", "Open javascript:alert(1)", "x onerror=alert(1)"])
async def test_script_patterns_are_refused_in_guide_text(client, db, headers, title):
    """Guide text is untrusted: the web renders it as text, and the script patterns of sanitize_string stay refused"""
    admin = await make_admin(db)
    h = headers(admin)
    for body, pointer in [({"title": title}, "#/title"), ({"title": "ok", "description": title}, "#/description")]:
        response = await client.post(GUIDES, headers=h, json=body)
        assert response.status_code == 422, response.text
        assert [item["code"] for item in response.json()["errors"] if item.get("pointer") == pointer] == [
            "text_script_pattern"]
    assert (await client.post(GUIDES, headers=h, json={"title": "How to create an observation"})).status_code == 201


@pytest.mark.parametrize("title", ["   ", "&nbsp;", " &nbsp; &#32; "])
async def test_a_title_that_is_empty_after_trimming_is_value_empty(client, db, headers, title):
    admin = await make_admin(db)
    created = await _create(client, headers, admin)
    for method, path in [("POST", GUIDES), ("PATCH", by_id(created["id"]))]:
        response = await client.request(method, path, headers=headers(admin), json={"title": title})
        assert response.status_code == 422, response.text
        assert [item["code"] for item in response.json()["errors"] if item.get("pointer") == "#/title"] == [
            "value_empty"]


async def test_an_empty_description_is_stored_as_null(client, db, headers):
    admin = await make_admin(db)
    body = await _create(client, headers, admin, description="   ")
    assert body["description"] is None and (await db.get(UserGuide, body["id"])).description is None
    filled = await client.patch(by_id(body["id"]), headers=headers(admin), json={"description": "Some text"})
    assert filled.json()["description"] == "Some text"
    emptied = await client.patch(by_id(body["id"]), headers=headers(admin), json={"description": " &nbsp; "})
    assert emptied.status_code == 200 and emptied.json()["description"] is None


@pytest.mark.parametrize("char", ["\u2028", "\u2029", "\u202a", "\u202b", "\u202c", "\u202d", "\u202e", "\u2066",
                                  "\u2067", "\u2068", "\u2069", "\u200e", "\u200f", "\u061c"])
async def test_a_title_refuses_separators_and_bidi_controls(client, db, headers, char):
    admin = await make_admin(db)
    response = await client.post(GUIDES, headers=headers(admin), json={"title": f"invoice{char}txt.exe"})
    assert response.status_code == 422, response.text
    assert [item["code"] for item in response.json()["errors"] if item.get("pointer") == "#/title"] == [
        "text_control_character"]


async def test_a_title_keeps_zwj_and_zwnj(client, db, headers):
    admin = await make_admin(db)
    title = "Joined\u200dword and non\u200cjoined"
    assert (await _create(client, headers, admin, title=title))["title"] == title


def test_plain_text_strips_and_limits():
    from app.backend.utils.validators import SecurityValidators
    assert SecurityValidators.plain_text("  Create the post  ", max_length=50) == "Create the post"
    assert SecurityValidators.plain_text("a\r\nb\tc", max_length=50, multiline=True) == "a\r\nb\tc"
    assert SecurityValidators.plain_text("SELECT, DROP <b>", max_length=50) == "SELECT, DROP <b>"
    for text, code in [("x" * 51, "string_too_long"), ("a\nb", "text_control_character"),
                       ("a\ud800b", "text_control_character"), ("a&#10;b", "text_control_character")]:
        with pytest.raises(PydanticCustomError) as refused:
            SecurityValidators.plain_text(text, max_length=50)
        assert refused.value.type == code, text


async def test_create_defaults(client, db, headers):
    admin = await make_admin(db)
    body = await _create(client, headers, admin)
    assert (body["description"], body["sort_order"], body["audience"]) == (None, 0, None)


@pytest.mark.parametrize("body, pointer", [
    ({}, "/title"),
    ({"title": ""}, "/title"),
    ({"title": "x" * 201}, "/title"),
    ({"title": "ok", "description": "x" * 2001}, "/description"),
    ({"title": "ok", "is_deleted": True}, "/is_deleted"),
    ({"title": "ok", "audience": {"kind": "root"}}, "/audience/kind"),
    ({"title": "ok", "audience": {"kind": "post"}}, "/audience/id"),
    ({"title": "ok", "audience": {"kind": "group", "id": 0}}, "/audience/id"),
])
async def test_create_refuses_bad_bodies(client, db, headers, body, pointer):
    admin = await make_admin(db)
    response = await client.post(GUIDES, headers=headers(admin), json=body)
    assert response.status_code == 422, response.text
    assert "#" + pointer in {item.get("pointer") for item in response.json()["errors"]}, response.json()


async def test_list_and_get_are_open_to_every_authenticated_user(client, db, headers):
    admin = await make_admin(db)
    created = await _create(client, headers, admin)
    operator = await _operator(db)

    listed = await client.get(GUIDES, headers=headers(operator))
    assert listed.status_code == 200, listed.text
    assert [item["id"] for item in listed.json()] == [created["id"]]
    one = await client.get(by_id(created["id"]), headers=headers(operator))
    assert one.status_code == 200 and one.json() == created

    assert (await client.get(GUIDES)).status_code == 401
    missing = await client.get(by_id(created["id"] + 1000), headers=headers(operator))
    assert missing.status_code == 404 and missing.json()["code"] == "guide_not_found"
    assert missing.json()["guide_id"] == created["id"] + 1000


@pytest.mark.parametrize("method, path, body, permission", [
    ("POST", collection, {"title": "x"}, "guide.create"),
    ("PATCH", by_id, {"title": "x"}, "guide.update"),
    ("POST", archive_path, None, "guide.archive"),
    ("POST", restore_path, None, "guide.archive"),
])
async def test_writes_need_the_system_permissions(client, db, headers, method, path, body, permission):
    admin = await make_admin(db)
    created = await _create(client, headers, admin)
    operator = await _operator(db)
    response = await client.request(method, path(created["id"]), headers=headers(operator), json=body)
    assert response.status_code == 403, response.text
    problem = response.json()
    assert problem["code"] == "permission_denied"
    assert problem["required"] == {"all": [permission], "any": []} and problem["missing"] == [permission]


async def test_list_order_and_title_filter(client, db, headers):
    admin = await make_admin(db)
    b = await _create(client, headers, admin, title="Beta", sort_order=1)
    a = await _create(client, headers, admin, title="Alpha", sort_order=1)
    first = await _create(client, headers, admin, title="Zulu first", sort_order=0)
    last = await _create(client, headers, admin, title="Alpha last", sort_order=5)
    h = headers(admin)

    listed = await client.get(GUIDES, headers=h)
    assert [item["id"] for item in listed.json()] == [first["id"], a["id"], b["id"], last["id"]]
    by_title = await client.get(GUIDES, headers=h, params={"sort_by": "title"})
    assert [item["id"] for item in by_title.json()] == [a["id"], last["id"], b["id"], first["id"]]
    descending = await client.get(GUIDES, headers=h, params={"sort_order": "descending"})
    assert [item["id"] for item in descending.json()] == [last["id"], a["id"], b["id"], first["id"]]

    found = await client.get(GUIDES, headers=h, params={"q": "ALPHA"})
    assert [item["id"] for item in found.json()] == [a["id"], last["id"]]
    page = await client.get(GUIDES, headers=h, params={"page": 2, "page_size": 3})
    assert [item["id"] for item in page.json()] == [last["id"]]
    bad = await client.get(GUIDES, headers=h, params={"sort_by": "created_by"})
    assert bad.status_code == 422


async def test_patch_changes_only_what_is_sent(client, db, headers):
    admin = await make_admin(db)
    created = await _create(client, headers, admin, description="Old", sort_order=2)
    response = await client.patch(by_id(created["id"]), headers=headers(admin), json={"title": "Renamed"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["title"], body["description"], body["sort_order"]) == ("Renamed", "Old", 2)

    cleared = await client.patch(by_id(created["id"]), headers=headers(admin), json={"description": None})
    assert cleared.status_code == 200 and cleared.json()["description"] is None

    empty = await client.patch(by_id(created["id"]), headers=headers(admin), json={})
    assert empty.status_code == 200 and empty.json() == cleared.json()
    assert [row.event for row in await _audit_rows(db, created["id"])] == [
        "user_guide.create", "user_guide.update", "user_guide.update"]


@pytest.mark.parametrize("body", [{"title": None}, {"sort_order": None}, {"is_deleted": True}])
async def test_patch_refuses_null_for_required_columns(client, db, headers, body):
    admin = await make_admin(db)
    created = await _create(client, headers, admin)
    response = await client.patch(by_id(created["id"]), headers=headers(admin), json=body)
    assert response.status_code == 422, response.text


async def test_audience_is_stored_as_a_scope_key(client, db, headers):
    admin = await make_admin(db)
    corps = await make_group(db, name="North corps", kind="corps")
    post = await make_post(db, name="Hill post")

    body = await _create(client, headers, admin, audience={"kind": "group", "id": corps.id})
    assert body["audience"] == {"kind": "group", "id": corps.id, "name": "North corps"}
    assert (await db.get(UserGuide, body["id"])).audience_key == f"g:{corps.id}"

    moved = await client.patch(by_id(body["id"]), headers=headers(admin),
                               json={"audience": {"kind": "post", "id": post.id}})
    assert moved.status_code == 200, moved.text
    assert moved.json()["audience"] == {"kind": "post", "id": post.id, "name": "Hill post"}

    kept = await client.patch(by_id(body["id"]), headers=headers(admin), json={"title": "Still for the post"})
    assert kept.json()["audience"] == {"kind": "post", "id": post.id, "name": "Hill post"}

    cleared = await client.patch(by_id(body["id"]), headers=headers(admin), json={"audience": None})
    assert cleared.status_code == 200 and cleared.json()["audience"] is None
    row = await db.get(UserGuide, body["id"])
    await db.refresh(row)
    assert row.audience_key is None


async def test_audience_name_is_the_name_now(client, db, headers):
    admin = await make_admin(db)
    group = await make_group(db, name="Old name")
    body = await _create(client, headers, admin, audience={"kind": "group", "id": group.id})
    group.name = "New name"
    db.add(group)
    await db.commit()
    again = await client.get(by_id(body["id"]), headers=headers(admin))
    assert again.json()["audience"]["name"] == "New name"


async def test_an_unknown_or_archived_audience_node_is_404(client, db, headers):
    admin = await make_admin(db)
    archived_post = await make_post(db)
    archived_post.is_deleted = True
    archived_group = await make_group(db)
    archived_group.is_deleted = True
    db.add_all([archived_post, archived_group])
    await db.commit()
    h = headers(admin)

    for audience, code in [
        ({"kind": "post", "id": 999999}, "post_not_found"),
        ({"kind": "post", "id": archived_post.id}, "post_not_found"),
        ({"kind": "group", "id": 999999}, "scope_group_not_found"),
        ({"kind": "group", "id": archived_group.id}, "scope_group_not_found"),
    ]:
        response = await client.post(GUIDES, headers=h, json={"title": "x", "audience": audience})
        assert response.status_code == 404 and response.json()["code"] == code, response.text
    assert (await db.exec(select(UserGuide))).all() == []

    created = await _create(client, headers, admin)
    response = await client.patch(by_id(created["id"]), headers=h,
                                  json={"audience": {"kind": "post", "id": archived_post.id}})
    assert response.status_code == 404 and response.json()["post_id"] == archived_post.id


async def test_archive_and_restore(client, db, headers):
    admin = await make_admin(db)
    created = await _create(client, headers, admin)
    h = headers(admin)

    archived = await client.post(archive_path(created["id"]), headers=h)
    assert archived.status_code == 200 and archived.json()["is_deleted"] is True, archived.text
    again = await client.post(archive_path(created["id"]), headers=h)
    assert again.status_code == 404 and again.json()["code"] == "guide_not_found"

    assert (await client.get(GUIDES, headers=h)).json() == []
    assert (await client.get(by_id(created["id"]), headers=h)).status_code == 404
    with_archived = await client.get(GUIDES, headers=h, params={"include_deleted": True})
    assert [item["id"] for item in with_archived.json()] == [created["id"]]
    one = await client.get(by_id(created["id"]), headers=h, params={"include_deleted": True})
    assert one.status_code == 200 and one.json()["is_deleted"] is True

    update = await client.patch(by_id(created["id"]), headers=h, json={"title": "x"})
    assert update.status_code == 404 and update.json()["code"] == "guide_not_found"

    restored = await client.post(restore_path(created["id"]), headers=h)
    assert restored.status_code == 200 and restored.json()["is_deleted"] is False, restored.text
    live = await client.post(restore_path(created["id"]), headers=h)
    assert live.status_code == 409 and live.json()["code"] == "guide_not_archived"
    unknown = await client.post(restore_path(created["id"] + 1000), headers=h)
    assert unknown.status_code == 404 and unknown.json()["code"] == "guide_not_found"


async def test_include_deleted_needs_guide_archive(client, db, headers):
    admin = await make_admin(db)
    created = await _create(client, headers, admin)
    operator = await _operator(db)
    for path in (GUIDES, by_id(created["id"])):
        response = await client.get(path, headers=headers(operator), params={"include_deleted": True})
        assert response.status_code == 403, response.text
        problem = response.json()
        assert problem["code"] == "permission_required_for_action"
        assert problem["missing"] == ["guide.archive"] and problem["action"] == "include_deleted"


async def test_every_write_writes_its_audit_row(client, db, headers):
    admin = await make_admin(db)
    created = await _create(client, headers, admin, title="First title")
    h = headers(admin)
    await client.patch(by_id(created["id"]), headers=h, json={"title": "Second title"})
    await client.post(archive_path(created["id"]), headers=h)
    await client.post(restore_path(created["id"]), headers=h)

    rows = await _audit_rows(db, created["id"])
    assert [(row.event, row.category, row.params) for row in rows] == [
        ("user_guide.create", "dictionaries", {"guide_title": "First title"}),
        ("user_guide.update", "dictionaries", {"guide_title": "Second title"}),
        ("user_guide.archive", "dictionaries", {"guide_title": "Second title"}),
        ("user_guide.restore", "dictionaries", {"guide_title": "Second title"}),
    ]
    assert all(row.user_id == admin.id and row.scope_key is None for row in rows)
    assert rows[1].old_values["title"] == "First title" and rows[1].new_values["title"] == "Second title"


async def test_a_refused_write_writes_no_audit_row(client, db, headers):
    admin = await make_admin(db)
    created = await _create(client, headers, admin)
    await client.patch(by_id(created["id"]), headers=headers(admin), json={"audience": {"kind": "post", "id": 999999}})
    await client.post(restore_path(created["id"]), headers=headers(admin))
    assert [row.event for row in await _audit_rows(db, created["id"])] == ["user_guide.create"]
