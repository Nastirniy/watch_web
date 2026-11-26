"""User guides (part 2): the tables, the system permissions guide.create / guide.update / guide.archive and the
audit events of guides and their files (task 4); the guide CRUD routes, the stored audience, archive and restore
(task 5); the files of a guide: upload, download, archive and restore (task 6). Who sees a guide (task 7):
tests/guides/test_guides_audience.py."""
import asyncio
import hashlib
import os
import time
from pathlib import Path
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
from app.backend.config import settings
from app.backend.enums.audit import AuditCategory, AuditEvent, audit_event
from app.backend.enums.general import BuiltinRole, PermissionLevel, Permissions, RoleKind, UserType
from app.backend.enums.routes import GuideRoutes
from app.backend.models import UserGuide, UserGuideFile
from app.backend.models.audit import AuditLog
from app.backend.models.media_format import MediaFormat
from app.backend.models.post import Post
from app.backend.models.role import Permission, Role, RolePermission
from app.backend.models.role_assignment import RoleAssignment, scope_columns
from app.backend.models.scope_group import ScopeGroup
from app.backend.models.system_role_assignment import SystemRoleAssignment
from app.backend.models.user import User
from app.backend.services.files import formats as file_formats
from app.backend.services.files import store as file_store
from tests.paths import FIXTURE_FILES
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
    """Guide text is untrusted: the web renders it as text, and the script patterns of plain_text stay refused"""
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
    assert problem["required"] == {"all": [permission], "any": [], "at_root": []} and problem["missing"] == [permission]


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


# --- files (task 6) --------------------------------------------------------------------------------------------------

FIXTURES = FIXTURE_FILES
PDF = (FIXTURES / "guide.pdf").read_bytes()
PREFIX = settings.storage.nginx_internal_uploads_prefix.rstrip("/")


def files_path(guide_id: int) -> str:
    return V1.path(GuideRoutes.FILES, guide_id=guide_id)


def download_path(guide_id: int, file_id: int) -> str:
    return V1.path(GuideRoutes.FILE_DOWNLOAD, guide_id=guide_id, file_id=file_id)


def file_archive_path(guide_id: int, file_id: int) -> str:
    return V1.path(GuideRoutes.FILE_ARCHIVE, guide_id=guide_id, file_id=file_id)


def file_restore_path(guide_id: int, file_id: int) -> str:
    return V1.path(GuideRoutes.FILE_RESTORE, guide_id=guide_id, file_id=file_id)


@pytest.fixture
def storage(tmp_path, monkeypatch):
    """The uploads root and the spool directory under tmp_path; the API serves files itself (no nginx)"""
    monkeypatch.setattr(settings.storage, "upload_path", str(tmp_path / "media"))
    monkeypatch.setattr(settings.storage, "temp_path", str(tmp_path / "spool"))
    monkeypatch.setattr(settings.storage, "enable_nginx", False)
    (tmp_path / "media").mkdir()
    return tmp_path


def _stored_files(storage: Path) -> list:
    """Every file and folder under <upload_path>/_guides/<guide_id>/ (temporary ones included), and every file of the
    spool directory"""
    found = []
    guides = storage / "media" / "_guides"
    if guides.exists():
        found += sorted(path.relative_to(storage).as_posix() for guide in guides.iterdir() for path in guide.rglob("*"))
    spool = storage / "spool"
    if spool.exists():
        found += sorted(path.relative_to(storage).as_posix() for path in spool.rglob("*") if path.is_file())
    return found


async def _upload(client, headers, user, guide_id: int, name: str = "guide.pdf", data: bytes = PDF,
                  content_type: str = "application/pdf", **form):
    return await client.post(files_path(guide_id), headers=headers(user), files={"file": (name, data, content_type)},
                             data=form)


async def _uploaded(client, headers, user, guide_id: int, **kwargs) -> dict:
    response = await _upload(client, headers, user, guide_id, **kwargs)
    assert response.status_code == 201, response.text
    return response.json()


async def _file_audit_rows(db, file_id: int):
    return (await db.exec(select(AuditLog).where(
        AuditLog.resource_type == "user_guide_file", AuditLog.resource_id == file_id).order_by(AuditLog.id))).all()


async def test_upload_a_pdf(client, db, headers, storage):
    admin = await make_admin(db)
    guide = await _create(client, headers, admin)
    body = await _uploaded(client, headers, admin, guide["id"], name="../Start guide.pdf", language="uk")

    # the name as names.sanitize_filename leaves it (the one sanitiser): no path, no spaces, ASCII stem
    assert body["file_name"] == "Start_guide.pdf" and body["content_type"] == "application/pdf"
    assert (body["size_bytes"], body["sha256"]) == (len(PDF), hashlib.sha256(PDF).hexdigest())
    assert (body["language"], body["is_deleted"]) == ("uk", False) and body["created_at"]
    assert body["download_url"] == download_path(guide["id"], body["id"])
    assert set(body) == {"id", "file_name", "content_type", "size_bytes", "sha256", "language", "created_at",
                         "download_url", "is_deleted"}

    row = await db.get(UserGuideFile, body["id"])
    assert row.file_path == f"_guides/{guide['id']}/{row.id}/{row.stored_name}"
    assert len(row.stored_name) == 36 and row.stored_name.endswith(".pdf")
    pdf_format = (await db.exec(select(MediaFormat.id).where(MediaFormat.content_type == "application/pdf"))).one()
    assert (row.guide_id, row.created_by, row.format_id) == (guide["id"], admin.id, pdf_format)
    assert (storage / "media" / row.file_path).read_bytes() == PDF
    folder = f"media/_guides/{guide['id']}/{row.id}"
    assert _stored_files(storage) == [folder, f"{folder}/{row.stored_name}"], "the spool and the .tmp- folder are gone"

    listed = (await client.get(by_id(guide["id"]), headers=headers(admin))).json()["files"]
    assert listed == [body]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
async def test_a_stored_file_is_0640_in_0750_folders(client, db, headers, storage):
    admin = await make_admin(db)
    guide = await _create(client, headers, admin)
    body = await _uploaded(client, headers, admin, guide["id"])
    row = await db.get(UserGuideFile, body["id"])
    path = storage / "media" / row.file_path
    assert path.stat().st_mode & 0o777 == 0o640
    for folder in (path.parent, path.parent.parent, path.parent.parent.parent):
        assert folder.stat().st_mode & 0o777 == 0o750, folder


async def test_download_is_an_attachment(client, db, headers, storage):
    admin = await make_admin(db)
    guide = await _create(client, headers, admin)
    body = await _uploaded(client, headers, admin, guide["id"], name="Start guide.pdf")
    operator = await _operator(db)

    response = await client.get(download_path(guide["id"], body["id"]), headers=headers(operator))
    assert response.status_code == 200, response.text
    assert response.content == PDF
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"] == (
        "attachment; filename=\"Start_guide.pdf\"; filename*=UTF-8''Start_guide.pdf")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-security-policy"] == "sandbox; default-src 'none'"
    assert response.headers["etag"] == f'"{body["sha256"]}"'
    assert response.headers["x-file-hash"] == body["sha256"]
    assert (await client.get(download_path(guide["id"], body["id"]))).status_code == 401


async def test_download_behind_nginx_hands_the_path_over(client, db, headers, storage, monkeypatch):
    admin = await make_admin(db)
    guide = await _create(client, headers, admin)
    body = await _uploaded(client, headers, admin, guide["id"])
    row = await db.get(UserGuideFile, body["id"])
    monkeypatch.setattr(settings.storage, "enable_nginx", True)

    response = await client.get(download_path(guide["id"], body["id"]), headers=headers(admin))
    assert response.status_code == 200, response.text
    assert response.content == b""
    assert response.headers["x-accel-redirect"] == f"{PREFIX}/_guides/{guide['id']}/{row.id}/{row.stored_name}"
    assert response.headers["content-disposition"].startswith("attachment; ")
    assert response.headers["content-type"] == "application/pdf"


@pytest.mark.parametrize("enable_nginx", [True, False])
async def test_a_file_missing_on_disk_is_guide_file_not_found(client, db, headers, storage, monkeypatch, enable_nginx):
    admin = await make_admin(db)
    guide = await _create(client, headers, admin)
    body = await _uploaded(client, headers, admin, guide["id"])
    row = await db.get(UserGuideFile, body["id"])
    (storage / "media" / row.file_path).unlink()
    monkeypatch.setattr(settings.storage, "enable_nginx", enable_nginx)

    response = await client.get(download_path(guide["id"], body["id"]), headers=headers(admin))
    assert response.status_code == 404 and response.json()["code"] == "guide_file_not_found", response.text
    assert row.stored_name not in response.text and "_guides" not in response.text
    assert "file_id" not in response.json(), "stored_file_response's missing= carries no params: absent, never null"


async def test_a_file_of_another_guide_or_unknown_is_404(client, db, headers, storage):
    admin = await make_admin(db)
    first = await _create(client, headers, admin)
    second = await _create(client, headers, admin, title="Second")
    body = await _uploaded(client, headers, admin, first["id"])
    h = headers(admin)
    for method, path, file_id in (("GET", download_path(second["id"], body["id"]), body["id"]),
                                  ("GET", download_path(first["id"], body["id"] + 1000), body["id"] + 1000),
                                  ("POST", file_archive_path(second["id"], body["id"]), body["id"]),
                                  ("POST", file_restore_path(second["id"], body["id"]), body["id"])):
        response = await client.request(method, path, headers=h)
        assert response.status_code == 404 and response.json()["code"] == "guide_file_not_found", (path, response.text)
        assert response.json()["file_id"] == file_id, response.text
    unknown = await client.get(download_path(first["id"] + 1000, body["id"]), headers=h)
    assert unknown.status_code == 404 and unknown.json()["code"] == "guide_not_found"


async def test_upload_to_an_archived_or_unknown_guide_is_404(client, db, headers, storage):
    admin = await make_admin(db)
    guide = await _create(client, headers, admin)
    await client.post(archive_path(guide["id"]), headers=headers(admin))
    for guide_id in (guide["id"], guide["id"] + 1000):
        response = await _upload(client, headers, admin, guide_id)
        assert response.status_code == 404 and response.json()["code"] == "guide_not_found", response.text
    assert (await db.exec(select(UserGuideFile))).all() == []
    assert _stored_files(storage) == []


async def test_file_writes_need_their_permissions(client, db, headers, storage):
    admin = await make_admin(db)
    guide = await _create(client, headers, admin)
    operator = await _operator(db)
    response = await _upload(client, headers, operator, guide["id"])
    assert response.status_code == 403, response.text
    assert response.json()["missing"] == ["guide.update"]
    for path in (file_archive_path(guide["id"], 1), file_restore_path(guide["id"], 1)):
        refused = await client.post(path, headers=headers(operator))
        assert refused.status_code == 403 and refused.json()["missing"] == ["guide.archive"], refused.text
    assert _stored_files(storage) == []


async def test_an_upload_above_the_format_limit_is_413(client, db, headers, storage):
    admin = await make_admin(db)
    guide = await _create(client, headers, admin)
    pdf = (await db.exec(select(MediaFormat).where(MediaFormat.content_type == "application/pdf"))).one()
    pdf.max_file_size_bytes = 100
    db.add(pdf)
    await db.commit()
    file_formats.invalidate_registry_cache()

    response = await _upload(client, headers, admin, guide["id"])
    assert response.status_code == 413, response.text
    problem = response.json()
    assert problem["code"] == "media_file_too_large"
    assert (problem["max_file_size_bytes"], problem["content_type"]) == (100, "application/pdf")
    assert (await db.exec(select(UserGuideFile))).all() == []
    assert _stored_files(storage) == []


@pytest.mark.parametrize("raw", [b"a\xed\xb2\x80.pdf", b"a\x80\xff.pdf", "\U0001f4fb guide.pdf".encode("utf-8"),
                                 "‮guide‍.pdf".encode("utf-8"), b"g" * 5000 + b".pdf"],
                         ids=["surrogate-bytes", "invalid-utf8", "non-bmp", "format-chars", "very-long"])
async def test_a_raw_file_name_of_a_guide_file(client, db, headers, storage, raw):
    """Any bytes in the part's file name: 201 with the name the sanitiser leaves, never a 500"""
    from app.backend.services.files.names import sanitize_filename
    from tests.media.test_upload_filenames_and_temp import MULTIPART, _raw_multipart

    admin = await make_admin(db)
    guide = await _create(client, headers, admin)
    response = await client.post(files_path(guide["id"]), content=_raw_multipart({}, raw, PDF, b"application/pdf"),
                                 headers={**MULTIPART, **headers(admin)})
    assert response.status_code == 201, response.text
    name = response.json()["file_name"]
    assert name.endswith(".pdf") and len(name) <= 200
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError:
        decoded = raw.decode("latin-1")
    assert name == sanitize_filename(decoded) or name.startswith("upload_")
    download = await client.get(download_path(guide["id"], response.json()["id"]), headers=headers(admin))
    assert download.status_code == 200 and download.content == PDF


async def test_an_upload_above_the_limit_the_store_reads_is_413(client, db, headers, storage, monkeypatch):
    """The store checks the size again against the registry it reads itself (a limit lowered while the body was
    arriving): 413 with that limit, no row, nothing left in the spool or the guide tree"""
    import dataclasses
    from app.backend.api.v1 import guides as guide_routes

    admin = await make_admin(db)
    guide = await _create(client, headers, admin)
    pdf = (await db.exec(select(MediaFormat).where(MediaFormat.content_type == "application/pdf"))).one()
    pdf.max_file_size_bytes = 100
    db.add(pdf)
    await db.commit()
    file_formats.invalidate_registry_cache()
    real_check = guide_routes.check_guide_format

    def check_before_the_change(registry, filename, content_type=None):
        """What the route saw before the limit was lowered"""
        return dataclasses.replace(real_check(registry, filename, content_type), max_file_size_bytes=10 * len(PDF))

    monkeypatch.setattr(guide_routes, "check_guide_format", check_before_the_change)
    response = await _upload(client, headers, admin, guide["id"])
    assert response.status_code == 413, response.text
    problem = response.json()
    assert problem["code"] == "media_file_too_large"
    assert (problem["max_file_size_bytes"], problem["file_size_bytes"], problem["content_type"]) == \
        (100, len(PDF), "application/pdf")
    assert (await db.exec(select(UserGuideFile))).all() == []
    assert _stored_files(storage) == []


@pytest.mark.parametrize("name, content_type, code", [
    ("setup.exe", "application/octet-stream", "media_extension_dangerous"),
    ("photo.png", "image/png", "media_extension_not_accepted"),
    ("guide", "application/pdf", "media_extension_not_accepted"),
    ("guide.pdf", "text/plain", "media_content_type_extension_mismatch"),
    ("guide.pdf", "image/png", "media_content_type_not_accepted"),
])
async def test_a_file_outside_the_document_formats_is_415(client, db, headers, storage, name, content_type, code):
    admin = await make_admin(db)
    guide = await _create(client, headers, admin)
    response = await _upload(client, headers, admin, guide["id"], name=name, content_type=content_type)
    assert response.status_code == 415, response.text
    assert response.json()["code"] == code
    assert response.json()["media_type"] == "document"
    assert "application/pdf" in response.headers["accept-post"] and "image/png" not in response.headers["accept-post"]
    assert (await db.exec(select(UserGuideFile))).all() == []
    assert _stored_files(storage) == []


async def test_bytes_that_are_not_the_format_are_415(client, db, headers, storage):
    admin = await make_admin(db)
    guide = await _create(client, headers, admin)
    html = (FIXTURES / "html_page.pdf").read_bytes()
    response = await _upload(client, headers, admin, guide["id"], data=html)
    assert response.status_code == 415, response.text
    assert (response.json()["code"], response.json()["content_type"]) == ("media_content_type_not_accepted",
                                                                          "text/html")
    assert _stored_files(storage) == []


async def test_an_empty_file_is_400(client, db, headers, storage):
    admin = await make_admin(db)
    guide = await _create(client, headers, admin)
    response = await _upload(client, headers, admin, guide["id"], data=b"")
    assert response.status_code == 400 and response.json()["code"] == "media_file_empty", response.text
    assert _stored_files(storage) == []


async def test_a_text_file_must_be_utf8(client, db, headers, storage):
    admin = await make_admin(db)
    guide = await _create(client, headers, admin)

    refused = await _upload(client, headers, admin, guide["id"], name="binary.md",
                            data=(FIXTURES / "binary.md").read_bytes(), content_type="text/markdown")
    assert refused.status_code == 415, refused.text
    assert (refused.json()["code"], refused.json()["content_type"]) == ("document_text_invalid", "text/markdown")
    utf16 = await _upload(client, headers, admin, guide["id"], name="guide.txt",
                          data=(FIXTURES / "utf16.txt").read_bytes(), content_type="text/plain")
    assert utf16.status_code == 415 and utf16.json()["code"] == "document_text_invalid", utf16.text
    assert _stored_files(storage) == []

    text = "\ufeff# \u0406\u043d\u0441\u0442\u0440\u0443\u043a\u0446\u0456\u044f\n\nUTF-8 with a BOM\n".encode("utf-8")
    body = await _uploaded(client, headers, admin, guide["id"], name="guide.md", data=text,
                           content_type="text/markdown")
    assert body["content_type"] == "text/markdown"
    served = await client.get(download_path(guide["id"], body["id"]), headers=headers(admin))
    assert served.status_code == 200 and served.content == text
    assert served.headers["content-type"] == "text/markdown; charset=UTF-8"
    plain = await _uploaded(client, headers, admin, guide["id"], name="guide.txt",
                            data=(FIXTURES / "guide.txt").read_bytes(), content_type="text/plain")
    served = await client.get(download_path(guide["id"], plain["id"]), headers=headers(admin))
    assert served.headers["content-type"] == "text/plain; charset=utf-8"


async def test_the_canonical_content_type_is_stored(client, db, headers, storage):
    """A .md file is text/plain to libmagic (an alias of Markdown): the row keeps the format's name"""
    admin = await make_admin(db)
    guide = await _create(client, headers, admin)
    body = await _uploaded(client, headers, admin, guide["id"], name="guide.md",
                           data=(FIXTURES / "guide.md").read_bytes(), content_type="application/octet-stream")
    assert body["content_type"] == "text/markdown"


@pytest.mark.parametrize("language", ["uk", "en", "en-US", "uk-UA", "zh-Hant", "es-419", "", None])
async def test_language_tags_that_are_accepted(client, db, headers, storage, language):
    admin = await make_admin(db)
    guide = await _create(client, headers, admin)
    form = {} if language is None else {"language": language}
    body = await _uploaded(client, headers, admin, guide["id"], **form)
    assert body["language"] == (language or None)


@pytest.mark.parametrize("language, code", [
    ("u", "language_tag_invalid"), ("english", "language_tag_invalid"), ("uk_UA", "language_tag_invalid"),
    ("uk-", "language_tag_invalid"), ("-uk", "language_tag_invalid"), ("uk--UA", "language_tag_invalid"),
    ("1k", "language_tag_invalid"), ("uk UA", "language_tag_invalid"), ("\u0443\u043a", "language_tag_invalid"),
    ("en-US-x-123", "string_too_long"),
])
async def test_language_tags_that_are_refused(client, db, headers, storage, language, code):
    admin = await make_admin(db)
    guide = await _create(client, headers, admin)
    response = await _upload(client, headers, admin, guide["id"], language=language)
    assert response.status_code == 422, response.text
    assert [(item["code"], item.get("pointer")) for item in response.json()["errors"]] == [(code, "#/language")]
    assert (await db.exec(select(UserGuideFile))).all() == []
    assert _stored_files(storage) == []


async def test_archived_files_are_hidden_and_not_served(client, db, headers, storage):
    admin = await make_admin(db)
    guide = await _create(client, headers, admin)
    kept = await _uploaded(client, headers, admin, guide["id"])
    gone = await _uploaded(client, headers, admin, guide["id"], name="old.pdf")
    h = headers(admin)

    archived = await client.post(file_archive_path(guide["id"], gone["id"]), headers=h)
    assert archived.status_code == 200, archived.text
    assert archived.json() == {**gone, "is_deleted": True}
    row = await db.get(UserGuideFile, gone["id"])
    assert (storage / "media" / row.file_path).read_bytes() == PDF, "archiving keeps the file on disk"

    operator = await _operator(db)
    for user in (operator, admin):
        one = (await client.get(by_id(guide["id"]), headers=headers(user))).json()
        assert [item["id"] for item in one["files"]] == [kept["id"]]
        listed = (await client.get(GUIDES, headers=headers(user))).json()
        assert [item["id"] for item in listed[0]["files"]] == [kept["id"]]
        response = await client.get(download_path(guide["id"], gone["id"]), headers=headers(user))
        assert response.status_code == 404 and response.json()["code"] == "guide_file_not_found", response.text

    with_archived = (await client.get(by_id(guide["id"]), headers=h, params={"include_deleted": True})).json()
    assert [(item["id"], item["is_deleted"]) for item in with_archived["files"]] == [
        (kept["id"], False), (gone["id"], True)]
    served = await client.get(download_path(guide["id"], gone["id"]), headers=h, params={"include_deleted": True})
    assert served.status_code == 200 and served.content == PDF

    refused = await client.get(download_path(guide["id"], gone["id"]), headers=headers(operator),
                               params={"include_deleted": True})
    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == "permission_required_for_action" and refused.json()["missing"] == ["guide.archive"]


async def test_files_of_an_archived_guide_are_served_only_with_include_deleted(client, db, headers, storage):
    admin = await make_admin(db)
    guide = await _create(client, headers, admin)
    body = await _uploaded(client, headers, admin, guide["id"])
    await client.post(archive_path(guide["id"]), headers=headers(admin))
    response = await client.get(download_path(guide["id"], body["id"]), headers=headers(admin))
    assert response.status_code == 404 and response.json()["code"] == "guide_not_found"
    served = await client.get(download_path(guide["id"], body["id"]), headers=headers(admin),
                              params={"include_deleted": True})
    assert served.status_code == 200 and served.content == PDF
    for path in (file_archive_path(guide["id"], body["id"]), file_restore_path(guide["id"], body["id"])):
        refused = await client.post(path, headers=headers(admin))
        assert refused.status_code == 404 and refused.json()["code"] == "guide_not_found", refused.text


async def test_archive_and_restore_a_file(client, db, headers, storage):
    admin = await make_admin(db)
    guide = await _create(client, headers, admin, title="Files guide")
    body = await _uploaded(client, headers, admin, guide["id"])
    h = headers(admin)

    live = await client.post(file_restore_path(guide["id"], body["id"]), headers=h)
    assert live.status_code == 409 and live.json()["code"] == "guide_file_not_archived", live.text
    assert (await client.post(file_archive_path(guide["id"], body["id"]), headers=h)).status_code == 200
    again = await client.post(file_archive_path(guide["id"], body["id"]), headers=h)
    assert again.status_code == 404 and again.json()["code"] == "guide_file_not_found", again.text
    assert again.json()["file_id"] == body["id"]
    restored = await client.post(file_restore_path(guide["id"], body["id"]), headers=h)
    assert restored.status_code == 200 and restored.json() == body, restored.text
    assert (await client.get(download_path(guide["id"], body["id"]), headers=h)).status_code == 200

    rows = await _file_audit_rows(db, body["id"])
    params = {"guide_title": "Files guide", "file_name": "guide.pdf", "size_bytes": len(PDF), "sha256": body["sha256"]}
    assert [(row.event, row.category, row.params) for row in rows] == [
        ("user_guide_file.upload", "dictionaries", params),
        ("user_guide_file.archive", "dictionaries", params),
        ("user_guide_file.restore", "dictionaries", params),
    ]
    assert all(row.user_id == admin.id and row.scope_key is None for row in rows)
    assert rows[0].old_values is None and rows[0].new_values["id"] == body["id"]
    assert (rows[1].old_values["is_deleted"], rows[1].new_values["is_deleted"]) == (False, True)


async def test_a_failed_store_leaves_no_file_and_no_row(client, db, headers, storage, monkeypatch):
    """An OSError in the rename of the .tmp- folder into place: 500 media_store_failed, nothing stays"""
    admin = await make_admin(db)
    guide = await _create(client, headers, admin)

    def storage_full(temporary: Path, target: Path) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(file_store, "_place", storage_full)
    response = await _upload(client, headers, admin, guide["id"])
    assert response.status_code == 500 and response.json()["code"] == "media_store_failed", response.text
    assert "space" not in response.text
    assert (await db.exec(select(UserGuideFile))).all() == []
    assert _stored_files(storage) == []
    assert (await db.exec(select(AuditLog).where(AuditLog.resource_type == "user_guide_file"))).all() == []


async def test_a_failed_audit_row_leaves_no_file_and_no_row(client, db, headers, storage, monkeypatch):
    """The file is in place before the commit: when the transaction fails, the folder goes too"""
    from sqlalchemy.exc import OperationalError
    from app.backend.api.v1 import guides as guide_routes

    admin = await make_admin(db)
    guide = await _create(client, headers, admin)

    async def audit_fails(*args, **kwargs):
        raise OperationalError("INSERT INTO audit_logs ...", {}, Exception("(1020, 'Record has changed')"))

    monkeypatch.setattr(guide_routes, "create_audit_log", audit_fails)
    response = await _upload(client, headers, admin, guide["id"])
    assert response.status_code == 500 and response.json()["code"] == "internal_error", response.text
    assert (await db.exec(select(UserGuideFile))).all() == []
    assert _stored_files(storage) == []


@pytest.mark.parametrize("failure", ["store", "audit"])
async def test_a_failed_first_upload_leaves_no_empty_guide_folder(client, db, headers, storage, monkeypatch, failure):
    """The guide's folder made for its first file goes with the file's folder when the upload fails; a guide folder
    that holds another file stays"""
    from sqlalchemy.exc import OperationalError
    from app.backend.api.v1 import guides as guide_routes

    admin = await make_admin(db)
    guide = await _create(client, headers, admin)
    guide_dir = storage / "media" / "_guides" / str(guide["id"])

    def storage_full(temporary: Path, target: Path) -> None:
        raise OSError(28, "No space left on device")

    async def audit_fails(*args, **kwargs):
        raise OperationalError("INSERT INTO audit_logs ...", {}, Exception("(1020, 'Record has changed')"))

    with monkeypatch.context() as patched:
        if failure == "store":
            patched.setattr(file_store, "_place", storage_full)
        else:
            patched.setattr(guide_routes, "create_audit_log", audit_fails)
        response = await _upload(client, headers, admin, guide["id"])
        assert response.status_code == 500, response.text
    assert (storage / "media" / "_guides").is_dir() and not guide_dir.exists()

    kept = await _uploaded(client, headers, admin, guide["id"])
    with monkeypatch.context() as patched:
        patched.setattr(file_store, "_place", storage_full)
        assert (await _upload(client, headers, admin, guide["id"], name="second.pdf")).status_code == 500
    row = await db.get(UserGuideFile, kept["id"])
    assert (storage / "media" / row.file_path).read_bytes() == PDF
    assert [p.name for p in guide_dir.iterdir()] == [str(row.id)]


def test_a_guide_folder_removed_by_another_cleanup_meanwhile_is_made_again(storage, monkeypatch):
    """The cleanup of a failed upload removes the guide's folder when empty: an upload that made it a moment before
    makes it again instead of failing"""
    real_make_dir = file_store._make_dir
    removed = []

    def make_dir_then_lose_it(path: Path) -> None:
        real_make_dir(path)
        if path.name == "5" and not removed:
            path.rmdir()                        # another upload's cleanup, between mkdir and the .tmp- folder
            removed.append(path)

    monkeypatch.setattr(file_store, "_make_dir", make_dir_then_lose_it)
    source = storage / "spooled.pdf"
    source.write_bytes(PDF)
    file_store._write_guide_folder(storage / "media", 5, 9, "stored.pdf", source)
    assert removed and (storage / "media" / "_guides" / "5" / "9" / "stored.pdf").read_bytes() == PDF


async def test_discard_removes_the_folder_even_when_the_rollback_fails(storage):
    """A lost connection fails the rollback: the folder is gone all the same (removed before it), the error
    propagates"""
    from sqlalchemy.exc import OperationalError

    folder = storage / "media" / "_guides" / "7" / "9"
    folder.mkdir(parents=True)
    (folder / "stored.pdf").write_bytes(PDF)

    class LostConnection:
        rolled_back = False

        async def rollback(self):
            self.rolled_back = True
            raise OperationalError("ROLLBACK", {}, Exception("(2013, 'Lost connection')"))

    session = LostConnection()
    row = UserGuideFile(id=9, guide_id=7, file_name="stored.pdf", stored_name="stored.pdf", file_path="",
                        content_type="application/pdf", format_id=1, size_bytes=len(PDF), sha256="ab" * 32,
                        created_by=1)
    with pytest.raises(OperationalError):
        await file_store.discard_guide_file(session, row)
    assert session.rolled_back and not folder.exists()


async def test_the_file_is_checked_and_hashed_before_the_guide_is_locked(client, db, headers, storage, monkeypatch):
    """The slow file work (the sniff, the text rule, the sha256 over the whole file) runs before the write takes the
    guide's lock, on the same spooled file the store then moves: the lock is held only for the insert, the folder and
    the commit"""
    from app.backend.services import guides as guides_service

    admin = await make_admin(db)
    guide = await _create(client, headers, admin)
    order = []
    real_sha256, real_detect, real_lock_first = file_store._sha256, file_store.detect_content_type, \
        guides_service.lock_first

    def sha256(path):
        order.append(("sha256", path))
        return real_sha256(path)

    def detect(path):
        order.append(("sniff", path))
        return real_detect(path)

    async def lock_first(session, query):
        order.append(("lock", None))
        return await real_lock_first(session, query)

    monkeypatch.setattr(file_store, "_sha256", sha256)
    monkeypatch.setattr(file_store, "detect_content_type", detect)
    monkeypatch.setattr(guides_service, "lock_first", lock_first)
    body = await _uploaded(client, headers, admin, guide["id"])
    assert [step for step, _ in order] == ["sniff", "sha256", "lock"], order
    assert order[0][1] == order[1][1], "one spooled file"
    assert body["sha256"] == hashlib.sha256(PDF).hexdigest()


async def test_two_uploads_to_one_guide_at_once_both_land(client, db, headers, storage):
    """Both requests run at once on the event loop; each locks the guide row first (lock_first), then inserts its
    row and writes its own folder. SQLite has no row locks (FOR UPDATE is left out) and the test engine has one
    connection, so this proves only that two uploads in flight share nothing: distinct rows, folders and stored
    names, both files whole and listed. The lock order itself is checked against MariaDB in the live run."""
    admin = await make_admin(db)
    guide = await _create(client, headers, admin)
    other = PDF + b"\n% a second file\n"
    first, second = await asyncio.gather(
        _upload(client, headers, admin, guide["id"], name="one.pdf"),
        _upload(client, headers, admin, guide["id"], name="two.pdf", data=other),
    )
    assert (first.status_code, second.status_code) == (201, 201), (first.text, second.text)
    rows = (await db.exec(select(UserGuideFile).order_by(UserGuideFile.id))).all()
    assert sorted(row.file_name for row in rows) == ["one.pdf", "two.pdf"]
    assert len({row.file_path for row in rows}) == 2
    for row in rows:
        expected = PDF if row.file_name == "one.pdf" else other
        assert (storage / "media" / row.file_path).read_bytes() == expected
        assert row.sha256 == hashlib.sha256(expected).hexdigest()
    listed = (await client.get(by_id(guide["id"]), headers=headers(admin))).json()["files"]
    assert len(listed) == 2


# --- ids reused after a restore (final review) -----------------------------------------------------------------------

def _age_tree(top: Path, seconds: float = 3600) -> None:
    """Every file and folder under top (top included) older than the reconcile age guard"""
    then = time.time() - seconds
    for path in sorted(top.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        os.utime(path, (then, then))
    os.utime(top, (then, then))


async def test_an_id_reused_after_reconcile_apply_stores_its_file(client, db, headers, storage):
    """A restore from a backup without the guide tables restarts their ids at 1; the folders of the files uploaded
    after the backup (one with its file, one emptied by an older reconcile) are moved aside by restore_reconcile
    --apply, and the next guide with the same ids stores its files"""
    from datetime import datetime, timezone
    from app.backend.cli.restore_reconcile import run

    media = storage / "media"
    (media / "_guides/1/1").mkdir(parents=True)
    (media / "_guides/1/1/old.pdf").write_bytes(b"uploaded after the backup")
    (media / "_guides/1/2").mkdir()
    _age_tree(media / "_guides")
    assert await run(db, apply=True, upload_path=media, store_dir=None, now=datetime.now(timezone.utc)) == 0
    assert not (media / "_guides/1").exists()

    admin = await make_admin(db)
    guide = await _create(client, headers, admin)
    first = await _uploaded(client, headers, admin, guide["id"])
    second = await _uploaded(client, headers, admin, guide["id"], name="second.pdf")
    assert (guide["id"], first["id"], second["id"]) == (1, 1, 2), "the ids the old folders had"
    for body in (first, second):
        row = await db.get(UserGuideFile, body["id"])
        assert (media / row.file_path).read_bytes() == PDF


async def test_an_empty_folder_left_at_the_file_id_is_reused(client, db, headers, storage):
    """No reconcile run: an empty folder of the new file's id is taken over"""
    admin = await make_admin(db)
    guide = await _create(client, headers, admin)
    leftover = storage / "media" / "_guides" / str(guide["id"]) / "1"
    leftover.mkdir(parents=True)
    body = await _uploaded(client, headers, admin, guide["id"])
    assert body["id"] == 1
    row = await db.get(UserGuideFile, body["id"])
    assert (storage / "media" / row.file_path).read_bytes() == PDF
    assert [p.name for p in leftover.iterdir()] == [row.stored_name]


def _set_aside(storage: Path) -> list:
    """Every file under <upload_path>/_orphaned/<time>/, relative to its time folder"""
    orphaned = storage / "media" / "_orphaned"
    if not orphaned.exists():
        return []
    return sorted(path.relative_to(stamp).as_posix() for stamp in orphaned.iterdir() for path in stamp.rglob("*")
                  if path.is_file())


async def test_a_folder_with_something_in_it_at_the_file_id_is_moved_aside(client, db, headers, storage):
    """A folder of the new file's id that holds anything (a file no row names, a NAS index folder such as @eaDir
    that a reconcile leaves in place) cannot be anyone's: no row has the new id. It moves to _orphaned/<time>/ as it
    is, never deleted, and the upload lands"""
    admin = await make_admin(db)
    guide = await _create(client, headers, admin)
    leftover = storage / "media" / "_guides" / str(guide["id"]) / "1"
    (leftover / "@eaDir").mkdir(parents=True)
    (leftover / "@eaDir" / "thumb.jpg").write_bytes(b"nas index")
    (leftover / "someone-else.pdf").write_bytes(b"not ours")
    body = await _uploaded(client, headers, admin, guide["id"])
    assert body["id"] == 1
    row = await db.get(UserGuideFile, body["id"])
    assert (storage / "media" / row.file_path).read_bytes() == PDF
    assert [p.name for p in leftover.iterdir()] == [row.stored_name]
    moved = f"_guides/{guide['id']}/1"
    assert _set_aside(storage) == [f"{moved}/@eaDir/thumb.jpg", f"{moved}/someone-else.pdf"]
    stamp = next((storage / "media" / "_orphaned").iterdir())
    assert (stamp / moved / "someone-else.pdf").read_bytes() == b"not ours"
    assert [p.name for p in leftover.parent.iterdir()] == ["1"], "no .tmp- folder left"


async def test_a_file_at_the_guide_folder_is_moved_aside(client, db, headers, storage):
    """A file where the guide's folder belongs (nothing the store makes) moves aside as well"""
    admin = await make_admin(db)
    guide = await _create(client, headers, admin)
    stray = storage / "media" / "_guides" / str(guide["id"])
    stray.parent.mkdir(parents=True)
    stray.write_bytes(b"a file, not a folder")
    body = await _uploaded(client, headers, admin, guide["id"])
    row = await db.get(UserGuideFile, body["id"])
    assert (storage / "media" / row.file_path).read_bytes() == PDF
    assert _set_aside(storage) == [f"_guides/{guide['id']}"]


async def test_a_nas_folder_left_by_reconcile_does_not_stop_a_reused_id(client, db, headers, storage):
    """After a restore restarts the guide ids: reconcile --apply moves the files no row names but leaves a file's
    folder holding a NAS index folder (@eaDir) where it is; the next file of that id still lands (the store moves
    the folder aside)"""
    from datetime import datetime, timezone
    from app.backend.cli.restore_reconcile import run

    media = storage / "media"
    (media / "_guides/1/1/@eaDir").mkdir(parents=True)
    (media / "_guides/1/1/@eaDir/old.pdf@SynoEAStream").write_bytes(b"nas")
    (media / "_guides/1/1/old.pdf").write_bytes(b"uploaded after the backup")
    _age_tree(media / "_guides")
    assert await run(db, apply=True, upload_path=media, store_dir=None, now=datetime.now(timezone.utc)) == 0
    assert (media / "_guides/1/1/@eaDir").is_dir() and not (media / "_guides/1/1/old.pdf").exists()

    admin = await make_admin(db)
    guide = await _create(client, headers, admin)
    body = await _uploaded(client, headers, admin, guide["id"])
    assert (guide["id"], body["id"]) == (1, 1)
    row = await db.get(UserGuideFile, body["id"])
    assert (media / row.file_path).read_bytes() == PDF
    assert "_guides/1/1/old.pdf" in _set_aside(storage)
    assert "_guides/1/1/@eaDir/old.pdf@SynoEAStream" in _set_aside(storage)
