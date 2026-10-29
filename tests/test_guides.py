"""User guides (part 2, task 4): the tables, the system permissions guide.create / guide.update / guide.archive and the
audit events of guides and their files. The routes come with later tasks."""
from typing import get_type_hints

import pytest
from sqlmodel import select

from app.backend.auth.builtin_roles import BUILTIN_ROLES
from app.backend.auth.context import load_auth_context
from app.backend.auth.scope import ROOT
from app.backend.auth.user_types import boundary
from app.backend.bootstrap.permissions import PERMISSION_META, permission_level
from app.backend.enums.audit import AuditCategory, AuditEvent, audit_event
from app.backend.enums.general import BuiltinRole, PermissionLevel, Permissions, RoleKind, UserType
from app.backend.models import UserGuide, UserGuideFile
from app.backend.models.role import Permission, Role, RolePermission
from app.backend.models.role_assignment import RoleAssignment, scope_columns
from app.backend.models.system_role_assignment import SystemRoleAssignment
from app.backend.models.user import User
from tests.helpers import builtin_role, make_admin

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
