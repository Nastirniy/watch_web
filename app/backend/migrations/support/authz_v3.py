"""Authorization v3 conversion: v2 data (role assignments on the scope tree, one role kind) mapped to tree roles,
system roles and system role assignments.

FROZEN once migration 030_authorization_v3 has shipped: 030, the authz_report command and the test factories run
exactly this code, so later changes to permissions, levels, built-in roles or user types never change what the
migration did. Standard library only; every set below is a copy of the live definition taken when the module was
written (PERMISSION_LEVELS in bootstrap/permissions.py, USER_TYPES in auth/user_types.py,
docs/superpowers/specs/2026-10-26-authorization-v3-design.md sections 1 to 3). Never import application code here and
never edit the copies; a different conversion is a new module.

Rules (spec section 7), over every role, assignment and user:

1. The built-in post_admin role is renamed in place to administrator (kind tree) and gets ALLOWED_GAIN: the new
   built-in administrator holds every tree permission (seeding would add them at the next start anyway).
2. The built-in admin role becomes the system role system_administrator in place: its tree permissions leave it
   (role_permissions_removed), its assignments leave role_assignments (moved_assignment_ids) and come back as system
   assignments; each holder also gets administrator at the root (added_root_administrators) unless they hold it.
3. A custom role named like a new built-in key (NEW_BUILTIN_KEYS, case-insensitive) is renamed <name>_custom.
4. Every other role gets a kind from its permissions: system only -> system (assignments moved as in 2); tree and
   system -> tree, the system permissions leave it for a new split role <name>_system (display name
   "<display name> (система)", unique case-insensitive against every name with _2, _3 ...; archived when the source
   is), and every root assignment of the source gets a system assignment of the split role; otherwise tree.
   Permissions this module does not know never decide a kind and stay where they are.
5. A system permission held below the root is not carried: a system role's assignment below the root is moved out
   and not re-created, a mixed role keeps its tree part there; both are reported (Dropped, system_below_root).
6. User types: TYPE_MAP. A grant that leaves the boundary of the new type is reported (Dropped, outside_boundary);
   the boundary keeps it out at evaluation.
"""
from dataclasses import dataclass
from types import MappingProxyType
from typing import Dict, FrozenSet, Iterable, List, Mapping, Optional, Sequence, Set, Tuple, Union

ROOT_KEY = "root"
TREE = "tree"
SYSTEM = "system"
ROLE_NAME_MAX = 100           # roles.name
ROLE_DISPLAY_NAME_MAX = 255   # roles.display_name
SPLIT_SUFFIX = "_system"
SPLIT_DISPLAY_SUFFIX = " (система)"
RENAME_SUFFIX = "_custom"
SYSTEM_BELOW_ROOT = "system_below_root"
OUTSIDE_BOUNDARY = "outside_boundary"

_SYSTEM_NAMES = frozenset({
    "audit.view", "backup.view", "category.archive", "category.create", "category.update", "certificate.export",
    "database.view", "media.monitor", "media_format.archive", "media_format.create", "media_format.update",
    "role.archive", "role.create", "role.delete", "role.update", "signal_type.archive", "signal_type.create",
    "signal_type.update", "tag.archive", "tag.create", "tag.update",
})
_TREE_NAMES = frozenset({
    "category.view", "certificate.create", "certificate.revoke", "certificate.view", "export.create", "media.archive",
    "media.create", "media.manage_any", "media.update", "media.view", "media_format.view", "observation.archive",
    "observation.create", "observation.manage_any", "observation.update", "observation.view", "post.archive",
    "post.create", "post.update", "post.view", "role.view", "scope_group.archive", "scope_group.create",
    "scope_group.update", "scope_group.view", "signal_type.view", "statistics.view", "tag.view", "user.archive",
    "user.create", "user.update", "user.view",
})
LEVELS: Mapping[str, str] = MappingProxyType(
    {**{name: TREE for name in _TREE_NAMES}, **{name: SYSTEM for name in _SYSTEM_NAMES}})

TYPE_MAP: Mapping[str, str] = MappingProxyType({"post_admin": "administrator", "admin": "system_administrator"})
BUILTIN_RENAME: Mapping[str, str] = MappingProxyType({"post_admin": "administrator", "admin": "system_administrator"})
NEW_BUILTIN_KEYS = ("administrator", "system_administrator", "auditor", "dictionary_editor", "tag_editor")
ALLOWED_GAIN = frozenset({"scope_group.create", "scope_group.update", "scope_group.archive"})

_VIEW = frozenset({"post.view", "observation.view", "media.view", "category.view", "signal_type.view", "tag.view"})
_AGGREGATES = frozenset({"statistics.view", "export.create"})
_OPERATOR = _VIEW | {"observation.create", "observation.update", "observation.archive", "media.create",
                     "media.archive", "media_format.view"}
_OVERRIDES = frozenset({"observation.manage_any", "media.manage_any"})
_SENIOR = _OPERATOR | _OVERRIDES | _AGGREGATES
_TAG_EDIT = frozenset({"tag.create", "tag.update"})
_DICTIONARY_EDIT = _TAG_EDIT | {"category.create", "category.update", "signal_type.create", "signal_type.update"}

TREE_BOUNDARY: Mapping[str, FrozenSet[str]] = MappingProxyType({
    "viewer": _VIEW | _AGGREGATES,
    "operator": _OPERATOR | _OVERRIDES | _AGGREGATES,
    "senior_operator": _SENIOR | {"media.update", "user.view"},
    "analyst": _VIEW | _AGGREGATES | {"user.view", "scope_group.view"},
    "administrator": _TREE_NAMES,
    "system_administrator": _TREE_NAMES,
})
SYSTEM_BOUNDARY: Mapping[str, FrozenSet[str]] = MappingProxyType({
    "viewer": frozenset(),
    "operator": _TAG_EDIT,
    "senior_operator": _TAG_EDIT,
    "analyst": frozenset(),
    "administrator": _DICTIONARY_EDIT,
    "system_administrator": _SYSTEM_NAMES,
})


@dataclass(frozen=True)
class V2Role:
    id: int
    name: str
    display_name: str
    is_deleted: bool
    builtin_key: Optional[str]
    permissions: FrozenSet[str]


@dataclass(frozen=True)
class V2Assignment:
    id: int
    user_id: int
    role_id: int
    scope_key: str


@dataclass(frozen=True)
class V2User:
    id: int
    user_type: str


@dataclass(frozen=True)
class SplitRole:
    """A new system role taking the system permissions of a mixed role (source_id)"""
    source_id: int
    name: str
    display_name: str
    is_deleted: bool
    permissions: FrozenSet[str]


@dataclass(frozen=True)
class SystemAssignmentPlan:
    """A system assignment to write: role is a role id, or the name of a SplitRole of the plan"""
    user_id: int
    role: Union[int, str]
    source_assignment_id: Optional[int]


@dataclass(frozen=True)
class Dropped:
    assignment_id: int
    permissions: FrozenSet[str]
    reason: str


@dataclass(frozen=True)
class V3Plan:
    role_kinds: Mapping[int, str]
    role_renames: Mapping[int, str]
    builtin_keys: Mapping[int, str]
    role_permissions_removed: Mapping[int, FrozenSet[str]]
    role_permissions_added: Mapping[int, FrozenSet[str]]
    split_roles: Tuple[SplitRole, ...]
    moved_assignment_ids: Tuple[int, ...]
    system_assignments: Tuple[SystemAssignmentPlan, ...]
    added_root_administrators: Tuple[int, ...]
    administrator_role_id: Optional[int]
    user_types: Mapping[int, str]
    dropped: Tuple[Dropped, ...]


def _system_part(permissions: FrozenSet[str]) -> FrozenSet[str]:
    return frozenset(name for name in permissions if LEVELS.get(name) == SYSTEM)


def _tree_part(permissions: FrozenSet[str]) -> FrozenSet[str]:
    return frozenset(name for name in permissions if LEVELS.get(name) == TREE)


class _Names:
    """Role names unique case-insensitive (the MySQL collation)"""

    def __init__(self, taken: Iterable[str]):
        self._taken: Set[str] = {name.lower() for name in taken}

    def free(self, wanted: str) -> str:
        base, n = wanted, 1
        name = base[:ROLE_NAME_MAX]
        while name.lower() in self._taken:
            n += 1
            suffix = f"_{n}"
            name = base[:ROLE_NAME_MAX - len(suffix)] + suffix
        self._taken.add(name.lower())
        return name


def plan_v3(roles: Mapping[int, V2Role], assignments: Sequence[V2Assignment], users: Sequence[V2User],
            taken_role_names: Iterable[str]) -> V3Plan:
    """The conversion of every role, assignment and user (module docstring)"""
    names = _Names(taken_role_names)
    kinds: Dict[int, str] = {}
    renames: Dict[int, str] = {}
    builtin_keys: Dict[int, str] = {}
    removed: Dict[int, FrozenSet[str]] = {}
    added: Dict[int, FrozenSet[str]] = {}
    splits: Dict[int, SplitRole] = {}
    administrator_id: Optional[int] = None
    admin_id: Optional[int] = None

    for role in sorted(roles.values(), key=lambda r: r.id):
        system, tree = _system_part(role.permissions), _tree_part(role.permissions)
        if role.builtin_key in BUILTIN_RENAME:
            builtin_keys[role.id] = BUILTIN_RENAME[role.builtin_key]
        if role.builtin_key == "post_admin":
            administrator_id = role.id
            kinds[role.id] = TREE
            gain = ALLOWED_GAIN - role.permissions
            if gain:
                added[role.id] = gain
            if system:
                removed[role.id] = system
            continue
        if role.builtin_key == "admin":
            admin_id = role.id
            kinds[role.id] = SYSTEM
            if tree:
                removed[role.id] = tree
            continue
        if role.builtin_key is None and role.name.lower() in NEW_BUILTIN_KEYS:
            renames[role.id] = names.free(role.name + RENAME_SUFFIX)
        if system and not tree:
            kinds[role.id] = SYSTEM
        else:
            kinds[role.id] = TREE
            if system:
                removed[role.id] = system
                base = renames.get(role.id, role.name)
                display = role.display_name[:ROLE_DISPLAY_NAME_MAX - len(SPLIT_DISPLAY_SUFFIX)] + SPLIT_DISPLAY_SUFFIX
                splits[role.id] = SplitRole(role.id, names.free(base + SPLIT_SUFFIX), display, role.is_deleted, system)

    moved: List[int] = []
    system_assignments: List[SystemAssignmentPlan] = []
    dropped: List[Dropped] = []
    for assignment in sorted(assignments, key=lambda a: a.id):
        role = roles.get(assignment.role_id)
        if role is None:
            continue
        at_root = assignment.scope_key == ROOT_KEY
        if kinds.get(role.id) == SYSTEM:
            moved.append(assignment.id)
            if at_root:
                system_assignments.append(SystemAssignmentPlan(assignment.user_id, role.id, assignment.id))
            else:
                dropped.append(Dropped(assignment.id, _system_part(role.permissions), SYSTEM_BELOW_ROOT))
        elif role.id in splits:
            if at_root:
                system_assignments.append(SystemAssignmentPlan(assignment.user_id, splits[role.id].name, assignment.id))
            else:
                dropped.append(Dropped(assignment.id, splits[role.id].permissions, SYSTEM_BELOW_ROOT))

    root_administrators: List[int] = []
    if admin_id is not None:
        holders = sorted({a.user_id for a in assignments if a.role_id == admin_id and a.scope_key == ROOT_KEY})
        has_administrator = {a.user_id for a in assignments
                             if a.role_id == administrator_id and a.scope_key == ROOT_KEY}
        root_administrators = [user_id for user_id in holders if user_id not in has_administrator]
        if root_administrators and administrator_id is None:
            raise ValueError("the database has holders of the built-in admin role but no built-in post_admin role: "
                             "start the application once at revision 028 to seed it")

    user_types = {user.id: TYPE_MAP.get(user.user_type, user.user_type) for user in users}
    plan = V3Plan(
        role_kinds=MappingProxyType(kinds), role_renames=MappingProxyType(renames),
        builtin_keys=MappingProxyType(builtin_keys), role_permissions_removed=MappingProxyType(removed),
        role_permissions_added=MappingProxyType(added), split_roles=tuple(splits[k] for k in sorted(splits)),
        moved_assignment_ids=tuple(moved), system_assignments=tuple(system_assignments),
        added_root_administrators=tuple(root_administrators), administrator_role_id=administrator_id,
        user_types=MappingProxyType(user_types), dropped=(),
    )
    dropped.extend(_outside_boundary(plan, roles, assignments))
    return V3Plan(**{**plan.__dict__, "dropped": tuple(dropped)})


def _v3_role_permissions(plan: V3Plan, role: V2Role) -> FrozenSet[str]:
    return (role.permissions - plan.role_permissions_removed.get(role.id, frozenset())
            | plan.role_permissions_added.get(role.id, frozenset()))


def _v3_grants(plan: V3Plan, roles: Mapping[int, V2Role], assignments: Sequence[V2Assignment],
               user_id: int) -> List[Tuple[Optional[int], str, str, FrozenSet[str], bool]]:
    """(assignment id or None, kind, scope key, role permissions, role archived) of the user after the plan"""
    moved = set(plan.moved_assignment_ids)
    splits = {split.name: split for split in plan.split_roles}
    grants = []
    for a in assignments:
        if a.user_id != user_id or a.id in moved or a.role_id not in roles:
            continue
        role = roles[a.role_id]
        grants.append((a.id, TREE, a.scope_key, _v3_role_permissions(plan, role), role.is_deleted))
    for s in plan.system_assignments:
        if s.user_id != user_id:
            continue
        if isinstance(s.role, str):
            split = splits[s.role]
            grants.append((None, SYSTEM, ROOT_KEY, split.permissions, split.is_deleted))
        else:
            role = roles[s.role]
            grants.append((None, SYSTEM, ROOT_KEY, _v3_role_permissions(plan, role), role.is_deleted))
    if user_id in plan.added_root_administrators and plan.administrator_role_id is not None:
        role = roles[plan.administrator_role_id]
        grants.append((None, TREE, ROOT_KEY, _v3_role_permissions(plan, role), role.is_deleted))
    return grants


def _bounded(user_type: str, kind: str, permissions: FrozenSet[str]) -> FrozenSet[str]:
    boundary = (TREE_BOUNDARY if kind == TREE else SYSTEM_BOUNDARY).get(user_type, frozenset())
    return frozenset(name for name in permissions if LEVELS.get(name) == kind and name in boundary)


def _outside_boundary(plan: V3Plan, roles: Mapping[int, V2Role],
                      assignments: Sequence[V2Assignment]) -> List[Dropped]:
    out = []
    for user_id, user_type in sorted(plan.user_types.items()):
        for assignment_id, kind, _, permissions, _ in _v3_grants(plan, roles, assignments, user_id):
            known = frozenset(name for name in permissions if LEVELS.get(name) == kind)
            left = known - _bounded(user_type, kind, known)
            if left and assignment_id is not None:
                out.append(Dropped(assignment_id, left, OUTSIDE_BOUNDARY))
    return out


def effective(plan: Optional[V3Plan], roles: Mapping[int, V2Role], assignments: Sequence[V2Assignment],
              users: Sequence[V2User], user_id: int) -> Dict[str, FrozenSet[str]]:
    """scope key -> the permissions in effect for the user: before the conversion (plan None: v2, the permissions of
    every live role at its node) or after it (v3: kinds, split roles, system assignments at the root, the boundary
    of the new type). Archived roles give nothing."""
    by_key: Dict[str, Set[str]] = {}
    if plan is None:
        for a in assignments:
            role = roles.get(a.role_id)
            if a.user_id == user_id and role is not None and not role.is_deleted:
                by_key.setdefault(a.scope_key, set()).update(role.permissions)
    else:
        user_type = plan.user_types.get(user_id) or TYPE_MAP.get(
            next(u.user_type for u in users if u.id == user_id), "")
        for _, kind, key, permissions, archived in _v3_grants(plan, roles, assignments, user_id):
            if not archived:
                by_key.setdefault(key, set()).update(_bounded(user_type, kind, permissions))
    return {key: frozenset(held) for key, held in by_key.items() if held}
