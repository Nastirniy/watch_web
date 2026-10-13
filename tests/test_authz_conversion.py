"""The frozen authorization v2 conversion (migrations/support/authz_v2.py), one test per rule of spec section 8.

Pure: legacy users and roles are built by hand, no database.
"""
import ast
import sys
from pathlib import Path
from typing import Dict, FrozenSet, Iterable

from app.backend.migrations.support import authz_v2 as conv
from app.backend.migrations.support.authz_v2 import (
    ConversionPlan, DerivedRole, LegacyRole, LegacyUser, PlannedAssignment, UserPlan, plan_conversion,
)

VIEW = frozenset({"post.view", "observation.view", "media.view", "category.view", "signal_type.view", "tag.view"})
OPERATOR = VIEW | {
    "observation.create", "observation.update", "observation.archive", "media.create", "media.archive",
    "media_format.view",
}


def role(role_id: int, perms: Iterable[str], name: str = None, deleted: bool = False) -> LegacyRole:
    return LegacyRole(id=role_id, name=name or f"r{role_id}", display_name=f"Role {role_id}", is_deleted=deleted,
                      permissions=frozenset(perms))


def user(user_id: int = 1, roles: Iterable[int] = (), post_id: int = None, access=()) -> LegacyUser:
    return LegacyUser(id=user_id, username=f"u{user_id}", post_id=post_id, role_ids=tuple(roles),
                      access=tuple(access))


def convert(users, roles, admin_role_id=None, taken=None) -> ConversionPlan:
    roles = {r.id: r for r in roles}
    return plan_conversion(users, roles, admin_role_id, [r.name for r in roles.values()] if taken is None else taken)


def one(u: LegacyUser, roles, admin_role_id=None, taken=None):
    plan = convert([u], roles, admin_role_id, taken)
    assert len(plan.users) == 1 and plan.users[0].user_id == u.id
    return plan, plan.users[0]


def by_name(plan: ConversionPlan) -> Dict[str, DerivedRole]:
    return {d.name: d for d in plan.derived_roles}


def permissions_at(plan: ConversionPlan, user_plan: UserPlan, roles) -> Dict[str, FrozenSet[str]]:
    """node key -> every permission the planned assignments put there"""
    roles = {r.id: r for r in roles}
    derived = by_name(plan)
    result: Dict[str, FrozenSet[str]] = {}
    for a in user_plan.assignments:
        perms = roles[a.role].permissions - conv.REMOVED if isinstance(a.role, int) else derived[a.role].permissions
        result[a.node] = result.get(a.node, frozenset()) | perms
    return result


# ----------------------------------------------------------------------- rule 1

def test_builtin_admin_holder_becomes_admin_at_root():
    admin = role(1, {"admin.true", "post.access_all", "tag.create", "observation.view"}, name="admin")
    other = role(2, {"observation.view"})
    plan, up = one(user(roles=(1, 2), post_id=7, access=((8, False),)), [admin, other], admin_role_id=1)
    assert up.user_type == "admin"
    assert up.assignments == (PlannedAssignment(role=1, node="root"),)
    assert up.differences == () and up.dropped == frozenset()
    assert plan.derived_roles == ()


def test_without_admin_role_id_rule_one_never_applies():
    admin = role(1, {"admin.true", "observation.view"}, name="admin")
    _, up = one(user(roles=(1,), post_id=7), [admin], admin_role_id=None)
    assert up.user_type == "viewer"
    assert up.assignments == (PlannedAssignment(role=1, node="p:7"),)


# ----------------------------------------------------------------------- rule 3

def test_access_all_puts_every_role_at_root():
    a = role(1, {"post.access_all", "observation.view", "observation.create", "user.update"}, name="a")
    b = role(2, {"media.view"}, name="b")
    plan, up = one(user(roles=(1, 2), post_id=7, access=((8, False), (9, True))), [a, b])
    assert {x.node for x in up.assignments} == {"root"}
    assert PlannedAssignment(role=2, node="root") in up.assignments   # b's set is exactly its stored set
    derived = by_name(plan)
    assert set(derived) == {"a_system"}
    assert derived["a_system"].permissions == {"observation.view", "observation.create"}
    assert PlannedAssignment(role="a_system", node="root") in up.assignments
    assert up.dropped == {"user.update"}   # dead without admin.true


def test_access_all_with_admin_true_keeps_gated_permissions_at_root():
    a = role(1, {"post.access_all", "admin.true", "observation.view", "user.update", "tag.create"}, name="a")
    plan, up = one(user(roles=(1,), post_id=7), [a])
    assert up.assignments == (PlannedAssignment(role=1, node="root"),)   # stored set minus REMOVED, as itself
    assert up.dropped == frozenset() and plan.derived_roles == ()


def test_role_holding_only_removed_permissions_is_not_assigned():
    a = role(1, {"post.access_all"})
    _, up = one(user(roles=(1,), post_id=7), [a])
    assert up.assignments == () and conv.DIFF_NO_PERMISSIONS in up.differences


# ----------------------------------------------------------------------- rule 4

def test_home_write_and_read_rows():
    ops = role(5, OPERATOR, name="ops")
    plan, up = one(user(roles=(5,), post_id=7, access=((8, True), (9, False))), [ops])
    assert set(up.assignments) == {
        PlannedAssignment(role=5, node="p:7"),
        PlannedAssignment(role=5, node="p:8"),
        PlannedAssignment(role="ops_view", node="p:9"),
    }
    view = by_name(plan)["ops_view"]
    assert view.permissions == {p for p in OPERATOR if p.endswith(".view")}
    assert view.source_role_id == 5 and view.display_name == "Role 5 (перегляд)" and not view.is_deleted
    assert up.user_type == "operator"
    assert up.dropped == frozenset()
    # observation.archive / media.archive stay at the post part: include_deleted no longer shows archived rows of
    # the read-only row
    assert up.differences == (conv.DIFF_ARCHIVED_RECORDS,)


def test_read_row_of_a_view_only_role_assigns_the_role_itself():
    reader = role(3, VIEW)
    plan, up = one(user(roles=(3,), post_id=7, access=((9, False),)), [reader])
    assert set(up.assignments) == {PlannedAssignment(3, "p:7"), PlannedAssignment(3, "p:9")}
    assert plan.derived_roles == ()


def test_access_row_on_the_home_post_adds_nothing():
    ops = role(5, OPERATOR, name="ops")
    plan, up = one(user(roles=(5,), post_id=7, access=((7, False),)), [ops])
    assert up.assignments == (PlannedAssignment(5, "p:7"),)
    assert plan.derived_roles == ()


def test_dead_permissions_dropped_without_admin_true():
    r = role(1, {"observation.view", "tag.create", "user.update"}, name="r")
    plan, up = one(user(roles=(1,), post_id=7), [r])
    assert up.dropped == {"tag.create", "user.update"}
    assert up.assignments == (PlannedAssignment(role="r_posts", node="p:7"),)
    assert by_name(plan)["r_posts"].permissions == {"observation.view"}
    assert by_name(plan)["r_posts"].display_name == "Role 1 (пости)"
    held = set().union(*permissions_at(plan, up, [r]).values())
    assert not held & {"tag.create", "user.update"}


def test_admin_true_role_is_split():
    r = role(1, {"admin.true", "post.view", "user.view", "tag.create", "observation.view"}, name="r")
    plan, up = one(user(roles=(1,), post_id=7), [r])
    derived = by_name(plan)
    assert set(up.assignments) == {PlannedAssignment("r_system", "root"), PlannedAssignment("r_posts", "p:7")}
    assert derived["r_system"].permissions == {"post.view", "user.view", "tag.create"}
    assert derived["r_system"].display_name == "Role 1 (система)"
    # a permission of the root part is never repeated in the post part
    assert derived["r_posts"].permissions == {"observation.view"}
    assert up.dropped == frozenset()


def test_admin_true_from_another_role_counts():
    flag = role(1, {"admin.true"}, name="flag")
    r = role(2, {"user.update", "observation.view"}, name="r")
    plan, up = one(user(roles=(1, 2), post_id=7), [flag, r])
    assert up.dropped == frozenset()
    assert set(up.assignments) == {PlannedAssignment("r_system", "root"), PlannedAssignment("r_posts", "p:7")}
    assert by_name(plan)["r_system"].permissions == {"user.update"}


def test_admin_true_keeps_record_administration_at_root():
    # R8: the record exception covered the administration of posts, users and the group list
    admin_perms = {"post.view", "post.create", "post.update", "post.archive", "user.view", "user.create",
                   "user.update", "user.archive", "scope_group.view", "statistics.view"}
    r = role(1, admin_perms | {"admin.true", "observation.view", "observation.create"}, name="r")
    plan, up = one(user(roles=(1,), post_id=7, access=((8, True), (9, False))), [r])
    derived = by_name(plan)
    assert derived["r_system"].permissions == admin_perms
    assert derived["r_posts"].permissions == {"observation.view", "observation.create"}
    assert derived["r_view"].permissions == {"observation.view"}
    assert set(up.assignments) == {
        PlannedAssignment("r_system", "root"), PlannedAssignment("r_posts", "p:7"),
        PlannedAssignment("r_posts", "p:8"), PlannedAssignment("r_view", "p:9"),
    }
    assert up.user_type == "post_admin"
    # archive held at the root: archived post and user records stay visible, nothing to report for them
    assert conv.DIFF_ARCHIVED_RECORDS not in up.differences


def test_record_administration_stays_dead_without_admin_true():
    r = role(1, {"post.view", "post.update", "user.view", "user.update", "observation.view"}, name="r")
    plan, up = one(user(roles=(1,), post_id=7), [r])
    assert up.assignments == (PlannedAssignment("r_posts", "p:7"),)
    assert by_name(plan)["r_posts"].permissions == {"post.view", "user.view", "observation.view"}
    assert up.dropped == {"post.update", "user.update"}


def test_view_part_carries_export_create():
    # R9: an export reads the data of read-only rows
    r = role(1, {"admin.true", "observation.view", "observation.create", "export.create"}, name="r")
    plan, up = one(user(roles=(1,), post_id=7, access=((9, False),)), [r])
    assert by_name(plan)["r_view"].permissions == {"observation.view", "export.create"}
    assert set(up.assignments) == {PlannedAssignment(1, "p:7"), PlannedAssignment("r_view", "p:9")}


def test_export_create_of_a_view_role_assigns_the_role_itself_at_read_rows():
    r = role(1, {"admin.true", "observation.view", "export.create"}, name="r")
    plan, up = one(user(roles=(1,), post_id=7, access=((9, False),)), [r])
    assert set(up.assignments) == {PlannedAssignment(1, "p:7"), PlannedAssignment(1, "p:9")}
    assert plan.derived_roles == ()


def test_admin_media_view_gets_media_monitor_at_root():
    # R10: storage statistics and poster status were media.view behind the admin gate
    r = role(1, {"admin.true", "media.view", "observation.view"}, name="r")
    plan, up = one(user(roles=(1,), post_id=7), [r])
    derived = by_name(plan)
    assert derived["r_system"].permissions == {"media.monitor"}   # R itself does not hold it: a derived role
    assert set(up.assignments) == {PlannedAssignment("r_system", "root"), PlannedAssignment(1, "p:7")}
    assert up.user_type == "admin"   # only the admin ceiling holds media.monitor
    # with post.access_all too
    plan, up = one(user(roles=(1,), post_id=7), [role(1, {"admin.true", "post.access_all", "media.view"}, "r")])
    assert by_name(plan)["r_system"].permissions == {"media.view", "media.monitor"}
    assert up.assignments == (PlannedAssignment("r_system", "root"),)
    # not without admin.true, and not for a role without media.view
    _, up = one(user(roles=(1,), post_id=7), [role(1, {"media.view"})])
    assert up.assignments == (PlannedAssignment(1, "p:7"),)
    flag, other = role(1, {"admin.true", "media.view"}, "flag"), role(2, {"observation.view"}, "other")
    plan, up = one(user(roles=(1, 2), post_id=7), [flag, other])
    assert PlannedAssignment(2, "p:7") in up.assignments
    assert by_name(plan)["flag_system"].permissions == {"media.monitor"}


def test_admin_true_of_an_archived_role_does_not_count():
    flag = role(1, {"admin.true"}, name="flag", deleted=True)
    r = role(2, {"user.update", "observation.view"}, name="r")
    plan, up = one(user(roles=(1, 2), post_id=7), [flag, r])
    assert up.dropped == {"user.update"}
    assert PlannedAssignment("r_posts", "p:7") in up.assignments


def test_user_without_post_keeps_dictionary_views_at_root():
    reader = role(1, {"category.view", "tag.view"}, name="dict")
    mixed = role(2, {"category.view", "media_format.view", "observation.view"}, name="mixed")
    plan, up = one(user(roles=(1, 2)), [reader, mixed])
    assert PlannedAssignment(1, "root") in up.assignments   # exactly its stored set
    assert PlannedAssignment("mixed_system", "root") in up.assignments
    assert by_name(plan)["mixed_system"].permissions == {"category.view", "media_format.view"}
    assert {a.node for a in up.assignments} == {"root"}
    # observation.view has no post to go to: not assigned, reported as dropped
    assert up.dropped == {"observation.view"}


def test_user_with_only_access_rows_keeps_dictionary_views_at_the_rows():
    mixed = role(2, {"category.view", "observation.view"}, name="mixed")
    _, up = one(user(roles=(2,), access=((9, False),)), [mixed])
    assert up.assignments == (PlannedAssignment(2, "p:9"),)


# ----------------------------------------------------------------------- rule 6

def test_type_inference_order_and_analyst_preference():
    def type_of(perms, access_all=False, home=7):
        extra = {"post.access_all"} if access_all else set()
        return one(user(roles=(1,), post_id=home), [role(1, set(perms) | extra)])[1].user_type

    assert type_of(VIEW) == "viewer"
    assert type_of(VIEW, access_all=True) == "analyst"   # both cover: post.access_all prefers analyst
    assert type_of(OPERATOR) == "operator"
    assert type_of(OPERATOR, access_all=True) == "operator"
    assert type_of(VIEW | {"user.view"}) == "senior_operator"   # first in order whose ceiling covers
    no_media = VIEW - {"media.view"}   # admin.true + media.view adds media.monitor: type admin
    assert type_of(no_media | {"admin.true", "scope_group.view"}) == "analyst"
    assert type_of(no_media | {"admin.true", "post.update"}) == "post_admin"
    assert type_of(no_media | {"admin.true", "audit.view"}) == "admin"
    assert type_of(VIEW | {"admin.true"}) == "admin"
    assert type_of(set()) == "viewer"
    assert type_of(set(), access_all=True) == "analyst"


def test_type_counts_archived_roles_too():
    active = role(1, VIEW)
    archived = role(2, OPERATOR, deleted=True)
    _, up = one(user(roles=(1, 2), post_id=7), [active, archived])
    assert up.user_type == "operator"


# ----------------------------------------------------------------------- archived roles

def test_archived_role_gives_archived_derived_roles():
    active = role(1, {"observation.view", "observation.create"}, name="active")
    archived = role(2, {"observation.view", "observation.update"}, name="old", deleted=True)
    plan, up = one(user(roles=(1, 2), post_id=7, access=((9, False),)), [active, archived])
    derived = by_name(plan)
    assert PlannedAssignment(2, "p:7") in up.assignments   # the archived role itself, still archived
    assert derived["old_view"].is_deleted and not derived["active_view"].is_deleted
    # same permission set, never shared between an active and an archived role
    assert derived["old_view"].permissions == derived["active_view"].permissions == {"observation.view"}
    assert set(up.assignments) == {
        PlannedAssignment(1, "p:7"), PlannedAssignment("active_view", "p:9"),
        PlannedAssignment(2, "p:7"), PlannedAssignment("old_view", "p:9"),
    }


# ----------------------------------------------------------------------- rule 5

def test_derived_roles_deduplicated_and_names_unique():
    ops = role(5, {"observation.view", "observation.create", "category.view"}, name="ops")
    other = role(6, {"category.view", "tag.create"}, name="other")
    users = [user(1, roles=(5,)), user(2, roles=(5,)), user(3, roles=(5,), post_id=7, access=((9, False),)),
             user(4, roles=(6,))]
    plan = convert(users, [ops, other], taken=["ops", "other", "ops_system"])
    derived = by_name(plan)
    # users 1 and 2 share one derived role; "ops_system" is taken
    assert set(derived) == {"ops_system_2", "ops_view"}
    assert derived["ops_system_2"].permissions == {"category.view"}
    assert plan.users[0].assignments == plan.users[1].assignments == (PlannedAssignment("ops_system_2", "root"),)
    # another role with the same set reuses the derived role
    assert plan.users[3].assignments == (PlannedAssignment("ops_system_2", "root"),)
    assert PlannedAssignment("ops_view", "p:9") in plan.users[2].assignments


def test_different_sets_of_one_role_get_numbered_names():
    flag = role(1, {"admin.true"}, name="flag")
    mix = role(2, {"category.view", "user.view", "observation.view"}, name="mix")
    plan = convert([user(1, roles=(1, 2), post_id=7), user(2, roles=(2,))], [flag, mix])
    derived = by_name(plan)
    assert derived["mix_system"].permissions == {"user.view"}       # admin.true: the record exception at root
    assert derived["mix_system_2"].permissions == {"category.view"}  # no post: dictionaries at root


def test_name_uniqueness_ignores_case_and_fits_the_column():
    long_name = "x" * 100
    r = role(1, {"category.view", "observation.view"}, name=long_name)
    plan = convert([user(1, roles=(1,))], [r], taken=[long_name, ("x" * 93 + "_system").upper()])
    (derived,) = plan.derived_roles
    assert len(derived.name) <= 100 and derived.name.endswith("_system_2")
    assert len(derived.display_name) <= 255


def test_users_are_planned_in_id_order_whatever_the_input_order():
    r = role(1, VIEW)
    plan = convert([user(3, roles=(1,), post_id=7), user(1, roles=(1,), post_id=7)], [r])
    assert [u.user_id for u in plan.users] == [1, 3]


# ----------------------------------------------------------------------- rule 7 and differences

def test_user_without_permissions_gets_no_assignment():
    _, up = one(user(roles=(1,), post_id=7), [role(1, set())])
    assert up.assignments == () and up.differences == (conv.DIFF_NO_PERMISSIONS,)
    _, nobody = one(user(roles=(), post_id=7), [])
    assert nobody.assignments == () and nobody.differences == (conv.DIFF_NO_PERMISSIONS,)


def test_differences_are_reported():
    def diffs(perms, home=7, access=()):
        return one(user(roles=(1,), post_id=home, access=access), [role(1, perms)])[1].differences

    assert diffs(OPERATOR) == ()
    assert conv.DIFF_UPLOAD_LIST in diffs({"admin.true", "media.view", "media.manage_any"})
    assert conv.DIFF_UPLOAD_LIST not in diffs({"admin.true", "post.access_all", "media.view", "media.manage_any"})
    # a read-only row was in the upload list too
    assert conv.DIFF_UPLOAD_LIST in diffs({"media.view", "media.manage_any"}, access=((9, False),))
    assert conv.DIFF_UPLOAD_LIST not in diffs({"media.view", "media.manage_any"}, access=((9, True),))

    assert conv.DIFF_COUNTERS in diffs({"admin.true", "post.view", "observation.view"})
    assert conv.DIFF_COUNTERS not in diffs({"post.view", "observation.view"})
    assert conv.DIFF_COUNTERS not in diffs({"admin.true", "post.access_all", "post.view", "observation.view"})

    assert conv.DIFF_ARCHIVED_RECORDS in diffs({"admin.true", "post.view"})
    assert conv.DIFF_ARCHIVED_RECORDS in diffs({"admin.true", "post.access_all", "user.view"})
    assert conv.DIFF_ARCHIVED_RECORDS not in diffs({"admin.true", "post.access_all", "post.view", "post.archive"})
    # R8: post.archive / user.archive of admin.true are at the root, archived records stay visible everywhere
    assert conv.DIFF_ARCHIVED_RECORDS not in diffs({"admin.true", "post.view", "post.archive", "user.view",
                                                    "user.archive"}, access=((9, False),))
    assert conv.DIFF_ARCHIVED_RECORDS not in diffs({"post.view", "observation.view", "observation.archive"})
    # include_deleted showed archived rows of read-only rows too
    assert conv.DIFF_ARCHIVED_RECORDS in diffs({"observation.view", "observation.archive"}, access=((9, False),))

    data = {"admin.true", "post.access_all", "observation.view", "observation.archive"}
    assert conv.DIFF_ARCHIVED_POST_DATA in diffs(data)
    assert conv.DIFF_ARCHIVED_POST_DATA not in diffs(data | {"post.archive"})
    assert conv.DIFF_ARCHIVED_POST_DATA not in diffs(data - {"admin.true"})

    # R8, R9, R10: administration, exports of read-only rows and storage monitoring are kept, nothing to report
    kept = {"admin.true", "post.update", "user.update", "scope_group.view", "observation.view", "export.create",
            "media.view"}
    assert diffs(kept, access=((9, False),)) == ()


def test_dropped_differences_are_gone():
    for name in ("DIFF_ADMINISTRATION", "DIFF_EXPORT_READ_ROWS", "DIFF_MONITOR"):
        assert not hasattr(conv, name)


def test_differences_are_sorted_and_unique():
    perms = {"admin.true", "post.view", "observation.view", "media.view", "media.manage_any", "user.update"}
    _, up = one(user(roles=(1,), post_id=7), [role(1, perms)])
    assert up.differences == (conv.DIFF_ARCHIVED_RECORDS, conv.DIFF_COUNTERS, conv.DIFF_UPLOAD_LIST)
    assert list(up.differences) == sorted(set(up.differences))


# ----------------------------------------------------------------------- frozen copies

def test_frozen_sets_equal_the_live_definitions():
    """The copies were taken from the live code when the module froze. Permissions added to the live code later
    (certificate.*, PKI) are unknown to them and left out of the comparison. If a later change of a permission the
    copies know makes this fail, delete this test: never edit the frozen copies."""
    from app.backend.auth.user_types import USER_TYPES
    from app.backend.bootstrap.permissions import PERMISSION_LEVELS
    from app.backend.enums.general import PermissionLevel, UserType

    # Every permission of the frozen catalog (the admin ceiling); admin.true and post.access_all leave the live code
    # with migration 023: compare without them
    known = conv.TYPE_CEILINGS["admin"]
    live_system = {name for name, level in PERMISSION_LEVELS.items() if level is PermissionLevel.SYSTEM}
    assert conv.SYSTEM - conv.REMOVED == (live_system & known) - conv.REMOVED and conv.ADMIN_TRUE in conv.SYSTEM
    assert conv.TYPE_ORDER == tuple(t.value for t in UserType)
    assert {key: ceiling - conv.REMOVED for key, ceiling in conv.TYPE_CEILINGS.items()} == {
        t.value: (spec.allowed_permissions & known) - conv.REMOVED for t, spec in USER_TYPES.items()
    }
    assert conv.REMOVED == {"admin.true", "post.access_all"}
    assert conv.DEAD_WITHOUT_ADMIN <= set(PERMISSION_LEVELS) and conv.GLOBAL_VIEW <= set(PERMISSION_LEVELS)


def test_frozen_module_uses_the_standard_library_only():
    source = Path(conv.__file__).read_text(encoding="utf-8")
    imported = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= set(sys.stdlib_module_names) | {"__future__"}, imported
