"""User types: permission levels, the type catalog (base and ceiling sets) and GET /user-types."""
from app.backend.api.v1 import V1
from app.backend.auth.user_types import USER_TYPES, excess_permissions
from app.backend.bootstrap.permissions import PERMISSION_LEVELS, PERMISSION_META, permission_level, role_level
from app.backend.enums.general import PermissionLevel, Permissions, UserType
from app.backend.enums.routes import UserTypeRoutes
from tests.helpers import make_user


def test_every_permission_declares_a_level():
    assert set(PERMISSION_LEVELS) == {p.value for p in Permissions}
    assert set(PERMISSION_META) == {p.value for p in Permissions}


def test_levels():
    assert permission_level("observation.view") == PermissionLevel.SCOPED
    assert permission_level("role.view") == PermissionLevel.SCOPED
    for name in ("role.create", "tag.archive", "media_format.update", "scope_group.create", "media.monitor",
                 "audit.view"):
        assert permission_level(name) == PermissionLevel.SYSTEM, name


def test_role_level():
    assert role_level(["observation.view"]) == PermissionLevel.SCOPED
    assert role_level([]) == PermissionLevel.SCOPED
    assert role_level(["observation.view", "tag.create"]) == PermissionLevel.SYSTEM


def test_ceiling_contains_base_for_every_type():
    assert list(USER_TYPES) == list(UserType)
    for spec in USER_TYPES.values():
        assert spec.base_permissions <= spec.allowed_permissions


def test_admin_holds_every_permission():
    spec = USER_TYPES[UserType.ADMIN]
    every = {p.value for p in Permissions}
    assert spec.base_permissions == every and spec.allowed_permissions == every


def test_operator_sets_match_spec():
    spec = USER_TYPES[UserType.OPERATOR]
    assert spec.base_permissions == {
        "post.view", "observation.view", "media.view", "category.view", "signal_type.view", "tag.view",
        "observation.create", "observation.update", "observation.archive", "media.create", "media.archive",
        "media_format.view",
    }
    assert spec.allowed_permissions == spec.base_permissions | {
        "observation.manage_any", "media.manage_any", "statistics.view", "export.create", "tag.create", "tag.update",
    }
    assert excess_permissions(UserType.OPERATOR, ["observation.view", "user.update"]) == {"user.update"}


def test_other_types_match_spec():
    view = {"post.view", "observation.view", "media.view", "category.view", "signal_type.view", "tag.view"}
    assert USER_TYPES[UserType.VIEWER].base_permissions == view
    assert USER_TYPES[UserType.VIEWER].allowed_permissions == view | {"statistics.view", "export.create"}
    assert USER_TYPES[UserType.ANALYST].base_permissions == view | {"statistics.view", "export.create"}
    assert USER_TYPES[UserType.ANALYST].allowed_permissions == (
        view | {"statistics.view", "export.create", "user.view", "scope_group.view"})
    operator = USER_TYPES[UserType.OPERATOR].base_permissions
    senior = USER_TYPES[UserType.SENIOR_OPERATOR]
    assert senior.base_permissions == operator | {
        "observation.manage_any", "media.manage_any", "statistics.view", "export.create"}
    assert senior.allowed_permissions == senior.base_permissions | {"media.update", "tag.create", "tag.update", "user.view"}
    post_admin = USER_TYPES[UserType.POST_ADMIN]
    assert post_admin.base_permissions == senior.base_permissions | {
        "media.update", "post.create", "post.update", "post.archive", "user.view", "user.create", "user.update",
        "user.archive", "role.view", "scope_group.view"}
    assert post_admin.allowed_permissions == post_admin.base_permissions | {
        "tag.create", "tag.update", "category.create", "category.update", "signal_type.create", "signal_type.update"}


async def test_user_types_catalog(client, db, headers):
    body = (await client.get(V1.path(UserTypeRoutes.ROOT), headers=headers(await make_user(db)))).json()
    assert [t["key"] for t in body] == ["viewer", "operator", "senior_operator", "analyst", "post_admin", "admin"]
    operator = body[1]
    assert operator["base_role"]["name"] == "operator" and operator["base_role"]["id"] > 0
    assert set(operator["base_permissions"]) <= set(operator["allowed_permissions"])
    assert operator["base_permissions"] == sorted(operator["base_permissions"])
    assert operator["display_name"] == "Оператор посту"
    assert len(body[5]["base_permissions"]) == len(Permissions)


async def test_user_types_need_authentication(client, db):
    response = await client.get(V1.path(UserTypeRoutes.ROOT))
    assert response.status_code == 401
