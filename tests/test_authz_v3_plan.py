"""The frozen conversion of authorization v2 data to v3 (migrations/support/authz_v3.py), on plain values."""
from app.backend.bootstrap.permissions import PERMISSION_LEVELS
from app.backend.enums.general import Permissions
from app.backend.migrations.support.authz_v3 import (
    ALLOWED_GAIN, LEVELS, SplitRole, SystemAssignmentPlan, V2Assignment, V2Role, V2User, effective, plan_v3,
)

# Permissions added after migration 030 (not in its frozen copy)
ADDED_AFTER_030 = frozenset({"audit.view_scope"})
ALL = frozenset(LEVELS)
TREE = frozenset(name for name, level in LEVELS.items() if level == "tree")
SYSTEM = ALL - TREE
POST_ADMIN_BASE = frozenset({
    "category.view", "certificate.create", "certificate.revoke", "certificate.view", "export.create", "media.archive",
    "media.create", "media.manage_any", "media.update", "media.view", "media_format.view", "observation.archive",
    "observation.create", "observation.manage_any", "observation.update", "observation.view", "post.archive",
    "post.create", "post.update", "post.view", "role.view", "scope_group.view", "signal_type.view", "statistics.view",
    "tag.view", "user.archive", "user.create", "user.update", "user.view",
})
OPERATOR_BASE = frozenset({
    "category.view", "media.archive", "media.create", "media.view", "media_format.view", "observation.archive",
    "observation.create", "observation.update", "observation.view", "post.view", "signal_type.view", "tag.view",
})


def _builtins():
    return {
        1: V2Role(1, "admin", "Адміністратор", False, "admin", ALL),
        2: V2Role(2, "post_admin", "Адміністратор посту", False, "post_admin", POST_ADMIN_BASE),
        3: V2Role(3, "operator", "Оператор посту", False, "operator", OPERATOR_BASE),
    }


def _plan(roles, assignments, users):
    return plan_v3(roles, assignments, users, [role.name for role in roles.values()])


def test_levels_copy_matches_spec():
    assert LEVELS["database.view"] == "system"
    assert LEVELS["scope_group.create"] == "tree"
    assert LEVELS["role.view"] == "tree"
    assert LEVELS["tag.create"] == "system"
    # the copy is frozen at migration 030: a permission added later is seeded with its level, never converted
    assert set(LEVELS) == {member.value for member in Permissions} - ADDED_AFTER_030
    assert all(LEVELS[name] == PERMISSION_LEVELS[name].value for name in LEVELS)


def test_admin_role_becomes_system_administrator():
    roles = _builtins()
    plan = _plan(roles, [V2Assignment(10, 1, 1, "root")], [V2User(1, "admin")])
    assert plan.builtin_keys[1] == "system_administrator"
    assert plan.role_kinds[1] == "system"
    assert plan.role_permissions_removed[1] == TREE
    assert 10 in plan.moved_assignment_ids
    assert SystemAssignmentPlan(1, 1, 10) in plan.system_assignments
    assert plan.added_root_administrators == (1,)
    assert plan.user_types[1] == "system_administrator"


def test_post_admin_role_renamed_in_place():
    roles = _builtins()
    plan = _plan(roles, [V2Assignment(11, 2, 2, "g:3")], [V2User(2, "post_admin")])
    assert plan.builtin_keys[2] == "administrator"
    assert plan.role_kinds[2] == "tree"
    assert 11 not in plan.moved_assignment_ids
    assert plan.role_permissions_added[2] == ALLOWED_GAIN
    assert plan.user_types[2] == "administrator"


def test_mixed_custom_role_split():
    roles = {**_builtins(), 20: V2Role(20, "ops", "Черговий", False, None, frozenset({"post.view", "audit.view"}))}
    plan = _plan(roles, [V2Assignment(30, 5, 20, "root")], [V2User(5, "admin")])
    assert plan.role_kinds[20] == "tree"
    assert plan.role_permissions_removed[20] == {"audit.view"}
    assert plan.split_roles == (SplitRole(20, "ops_system", "Черговий (система)", False, frozenset({"audit.view"})),)
    assert SystemAssignmentPlan(5, "ops_system", 30) in plan.system_assignments
    assert 30 not in plan.moved_assignment_ids


def test_system_only_custom_role_moves():
    roles = {**_builtins(), 21: V2Role(21, "aud", "Аудит", False, None, frozenset({"audit.view"}))}
    plan = _plan(roles, [V2Assignment(31, 5, 21, "root")], [V2User(5, "admin")])
    assert plan.role_kinds[21] == "system"
    assert 31 in plan.moved_assignment_ids
    assert SystemAssignmentPlan(5, 21, 31) in plan.system_assignments


def test_split_name_unique():
    roles = {**_builtins(),
             20: V2Role(20, "ops", "Черговий", False, None, frozenset({"post.view", "audit.view"})),
             22: V2Role(22, "OPS_system", "Інша", False, None, frozenset({"post.view"}))}
    plan = _plan(roles, [V2Assignment(30, 5, 20, "root")], [V2User(5, "admin")])
    assert plan.split_roles[0].name == "ops_system_2"


def test_collision_with_new_builtin_key_renamed():
    roles = {**_builtins(), 23: V2Role(23, "Auditor", "Мій аудитор", False, None, frozenset({"post.view"}))}
    plan = _plan(roles, [], [])
    assert plan.role_renames[23] == "Auditor_custom"


def test_system_permission_below_root_dropped():
    roles = {**_builtins(),
             24: V2Role(24, "bad", "Погана", False, None, frozenset({"post.view", "audit.view"})),
             25: V2Role(25, "worse", "Гірша", False, None, frozenset({"audit.view"}))}
    plan = _plan(roles, [V2Assignment(32, 6, 24, "p:7"), V2Assignment(33, 6, 25, "p:7")], [V2User(6, "operator")])
    dropped = {d.assignment_id: d for d in plan.dropped}
    assert dropped[32].permissions == {"audit.view"} and dropped[32].reason == "system_below_root"
    assert dropped[33].reason == "system_below_root"
    assert 33 in plan.moved_assignment_ids
    assert all(s.source_assignment_id not in (32, 33) for s in plan.system_assignments)


def test_effective_equal_except_allowed_gain():
    roles = _builtins()
    assignments = [V2Assignment(40, 7, 2, "g:3"), V2Assignment(41, 8, 3, "p:7"), V2Assignment(42, 9, 1, "root")]
    users = [V2User(7, "post_admin"), V2User(8, "operator"), V2User(9, "admin")]
    plan = _plan(roles, assignments, users)
    for user_id in (7, 8, 9):
        before, after = effective(None, roles, assignments, users, user_id), effective(plan, roles, assignments, users, user_id)
        assert set(before) == set(after), user_id
        for key in before:
            assert before[key] <= after[key], (user_id, key)
            assert after[key] - before[key] <= ALLOWED_GAIN, (user_id, key)
    assert effective(plan, roles, assignments, users, 7)["g:3"] - effective(None, roles, assignments, users, 7)["g:3"] == ALLOWED_GAIN
    assert effective(plan, roles, assignments, users, 9)["root"] == ALL


def test_archived_role_split_is_archived():
    roles = {**_builtins(), 26: V2Role(26, "old", "Стара", True, None, frozenset({"post.view", "audit.view"}))}
    plan = _plan(roles, [V2Assignment(34, 5, 26, "root")], [V2User(5, "admin")])
    assert plan.split_roles[0].is_deleted is True
    assert effective(plan, roles, [V2Assignment(34, 5, 26, "root")], [V2User(5, "admin")], 5) == {}
