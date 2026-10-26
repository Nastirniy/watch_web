"""Authorization v3 catalog: built-in roles of two kinds, user types as positions with a boundary, seeding of
levels and kinds, system role assignments as grants at the root."""
from sqlmodel import select

from app.backend.auth.builtin_roles import BUILTIN_ROLES
from app.backend.auth.context import load_auth_context
from app.backend.auth.scope import ROOT
from app.backend.auth.user_types import USER_TYPES, boundary
from app.backend.bootstrap.permissions import PERMISSION_LEVELS, permission_level, role_kind_of
from app.backend.enums.general import BuiltinRole, PermissionLevel, RoleKind, UserType
from app.backend.models.role import Permission, Role
from app.backend.models.role_assignment import RoleAssignment
from app.backend.models.system_role_assignment import SystemRoleAssignment
from tests.helpers import make_admin, make_user

TREE_NAMES = frozenset(name for name, level in PERMISSION_LEVELS.items() if level is PermissionLevel.TREE)
SYSTEM_NAMES = frozenset(name for name, level in PERMISSION_LEVELS.items() if level is PermissionLevel.SYSTEM)


def test_builtin_roles_hold_only_their_kind():
    assert set(BUILTIN_ROLES) == set(BuiltinRole)
    for key, spec in BUILTIN_ROLES.items():
        assert spec.permissions, key
        assert {permission_level(name).value for name in spec.permissions} == {spec.kind.value}, key


def test_administrator_holds_every_tree_permission():
    assert BUILTIN_ROLES[BuiltinRole.ADMINISTRATOR].permissions == TREE_NAMES
    assert BUILTIN_ROLES[BuiltinRole.ADMINISTRATOR].kind is RoleKind.TREE


def test_system_administrator_holds_every_system_permission():
    assert BUILTIN_ROLES[BuiltinRole.SYSTEM_ADMINISTRATOR].permissions == SYSTEM_NAMES
    assert BUILTIN_ROLES[BuiltinRole.SYSTEM_ADMINISTRATOR].kind is RoleKind.SYSTEM


def test_dictionary_editor_has_no_archive():
    editor = BUILTIN_ROLES[BuiltinRole.DICTIONARY_EDITOR].permissions
    assert editor == {"category.create", "category.update", "signal_type.create", "signal_type.update",
                      "tag.create", "tag.update"}
    assert BUILTIN_ROLES[BuiltinRole.TAG_EDITOR].permissions == {"tag.create", "tag.update"}
    assert BUILTIN_ROLES[BuiltinRole.AUDITOR].permissions == {"audit.view"}


def test_base_roles_fit_boundary():
    assert list(USER_TYPES) == list(UserType)
    for user_type, spec in USER_TYPES.items():
        assert spec.base_roles, user_type
        for base in spec.base_roles:
            role = BUILTIN_ROLES[base.role]
            assert role.permissions <= boundary(user_type, role.kind), (user_type, base.role)


def test_boundaries_match_spec():
    view = {"post.view", "observation.view", "media.view", "category.view", "signal_type.view", "tag.view"}
    operator = view | {"observation.create", "observation.update", "observation.archive", "media.create",
                       "media.archive", "media_format.view"}
    assert boundary(UserType.OPERATOR, RoleKind.TREE) == operator | {
        "observation.manage_any", "media.manage_any", "statistics.view", "export.create"}
    assert boundary(UserType.OPERATOR, RoleKind.SYSTEM) == {"tag.create", "tag.update"}
    assert boundary(UserType.VIEWER, RoleKind.SYSTEM) == frozenset()
    assert boundary(UserType.ADMINISTRATOR, RoleKind.TREE) == TREE_NAMES
    assert boundary(UserType.ADMINISTRATOR, RoleKind.SYSTEM) == {
        "category.create", "category.update", "signal_type.create", "signal_type.update", "tag.create", "tag.update"}
    assert boundary(UserType.SYSTEM_ADMINISTRATOR, RoleKind.SYSTEM) == SYSTEM_NAMES
    system_admin = USER_TYPES[UserType.SYSTEM_ADMINISTRATOR].base_roles
    assert [(b.role, b.at_root) for b in system_admin] == [
        (BuiltinRole.ADMINISTRATOR, True), (BuiltinRole.SYSTEM_ADMINISTRATOR, True)]


def test_no_type_named_after_a_place():
    assert [t.value for t in UserType] == [
        "viewer", "operator", "senior_operator", "analyst", "administrator", "system_administrator"]


def test_role_kind_of():
    assert role_kind_of(["post.view"]) is RoleKind.TREE
    assert role_kind_of(["audit.view"]) is RoleKind.SYSTEM
    assert role_kind_of(["post.view", "audit.view"]) is None
    assert role_kind_of([]) is None


async def test_seeding_creates_nine_builtin_roles_with_kinds(db):
    rows = (await db.exec(select(Role).where(Role.builtin_key.is_not(None)))).all()
    assert {row.builtin_key: row.kind for row in rows} == {key.value: spec.kind.value
                                                            for key, spec in BUILTIN_ROLES.items()}


async def test_seeding_writes_permission_levels(db):
    rows = (await db.exec(select(Permission.name, Permission.level))).all()
    assert dict(rows) == {name: level.value for name, level in PERMISSION_LEVELS.items()}


async def test_admin_factory_is_a_system_administrator(db):
    admin = await make_admin(db)
    assert admin.user_type == UserType.SYSTEM_ADMINISTRATOR.value
    tree = (await db.exec(select(Role.builtin_key, RoleAssignment.scope_key).select_from(RoleAssignment).join(
        Role, Role.id == RoleAssignment.role_id).where(RoleAssignment.user_id == admin.id))).all()
    assert tree == [("administrator", "root")]
    system = (await db.exec(select(Role.builtin_key).select_from(SystemRoleAssignment).join(
        Role, Role.id == SystemRoleAssignment.role_id).where(SystemRoleAssignment.user_id == admin.id))).all()
    assert system == ["system_administrator"]


async def test_system_assignment_is_a_root_grant(db):
    user = await make_user(db, user_type=UserType.SYSTEM_ADMINISTRATOR)
    auditor = (await db.exec(select(Role).where(Role.builtin_key == BuiltinRole.AUDITOR.value))).one()
    db.add(SystemRoleAssignment(user_id=user.id, role_id=auditor.id))
    await db.commit()
    auth = await load_auth_context(db, user)
    assert auth.has_at_root("audit.view") and "audit.view" in auth.held_anywhere
    (grant,) = [g for g in auth.grants if g.role_id == auditor.id]
    assert grant.kind == "system" and grant.node == ROOT
