"""Unit tests of the Require evaluation (no database)."""
import pytest

from app.backend.enums.errors import AccessErrorCode, RequiredPermissions
from app.backend.enums.general import Permissions
from app.backend.errors import openapi
from app.backend.auth.context import AuthContext
from app.backend.auth.require import PermissionCheckResult, Require, require
from app.backend.auth.scope import Grant, Node, ROOT

def grant(node: Node, *perms: str) -> Grant:
    return Grant(1, "role", "Role", False, node, frozenset(perms))


def ctx(*perms: str) -> AuthContext:
    """The permissions held at the root"""
    return AuthContext(user=None, grants=(grant(ROOT, *perms),))


def test_all_requires_every_permission():
    spec = Require.of(all=[Permissions.TAG_VIEW, Permissions.TAG_UPDATE])
    assert spec.evaluate(ctx("tag.view", "tag.update")).allowed
    result = spec.evaluate(ctx("tag.view"))
    assert not result.allowed
    assert result.missing == ("tag.update",)


def test_any_requires_one_permission():
    spec = Require.of(any=[Permissions.TAG_VIEW, Permissions.TAG_UPDATE])
    assert spec.evaluate(ctx("tag.update")).allowed
    result = spec.evaluate(ctx())
    assert not result.allowed
    assert set(result.missing) == {"tag.view", "tag.update"}


def test_there_is_no_admin_gate():
    """Require has only all / any: the admin flag, its parameter and admin_missing are gone, and admin.true (gone from
    the permission catalog; a name no grant satisfies) satisfies nothing"""
    with pytest.raises(TypeError):
        require(all=[Permissions.TAG_ARCHIVE], admin=True)
    with pytest.raises(TypeError):
        Require.of(all=[Permissions.TAG_ARCHIVE], admin=True)
    assert set(PermissionCheckResult.__dataclass_fields__) == {"allowed", "missing"}
    assert set(Require.__dataclass_fields__) == {"all", "any"}
    assert "admin_required" not in {code.value for code in AccessErrorCode}
    assert not hasattr(openapi, "ADMIN") and openapi.GUARDED == (AccessErrorCode.PERMISSION_DENIED,)
    assert set(RequiredPermissions.model_fields) == {"all", "any"}

    spec = Require.of(all=[Permissions.TAG_ARCHIVE])
    admin_only = spec.evaluate(ctx())
    assert not admin_only.allowed and admin_only.missing == ("tag.archive",)
    assert "admin.true" not in {p.value for p in Permissions}
    assert spec.evaluate(ctx("tag.archive")).allowed


def test_the_built_in_admin_role_passes_by_its_permissions():
    """is_admin is the built-in admin role at the root; require() never reads it"""
    spec = Require.of(all=[Permissions.OBSERVATION_VIEW])
    flag_only = AuthContext(user=None, grants=(Grant(1, "admin", "Admin", True, ROOT, frozenset()),))
    assert flag_only.is_admin and not spec.evaluate(flag_only).allowed
    below_root = AuthContext(user=None, grants=(Grant(1, "admin", "Admin", True, Node.post(7), frozenset({"observation.view"})),))
    assert not below_root.is_admin and spec.evaluate(below_root).allowed


def test_empty_require_only_needs_authentication():
    assert Require.of().evaluate(ctx()).allowed


def test_as_dict_shape():
    assert Require.of(all=["a"], any=["b"]).as_dict() == {"all": ["a"], "any": ["b"]}


def test_evaluation_reads_the_grants():
    """A permission counts when it is held at some node (permissions is held_anywhere)"""
    spec = Require.of(all=[Permissions.POST_VIEW, Permissions.POST_CREATE])
    at_a_post = AuthContext(user=None, grants=(grant(Node.post(7), "post.view"),))
    result = spec.evaluate(at_a_post)
    assert not result.allowed and result.missing == ("post.create",)
    assert at_a_post.permissions == at_a_post.held_anywhere == {"post.view"}

    at_a_group = AuthContext(user=None, grants=(grant(Node.group(3), "post.view", "post.create"),))
    assert spec.evaluate(at_a_group).allowed
    assert Require.of(any=[Permissions.TAG_VIEW, Permissions.POST_CREATE]).evaluate(at_a_group).allowed


def test_held_anywhere_is_computed_once_per_context():
    auth = ctx("tag.view", "tag.update")
    assert auth.held_anywhere == {"tag.view", "tag.update"}
    assert auth.held_anywhere is auth.held_anywhere
