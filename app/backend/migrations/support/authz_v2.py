"""Authorization v2 conversion: the old access model mapped to role assignments on the scope tree.

FROZEN once migration 023_authorization_v2 has shipped: 023 and the authz_report command run exactly this code,
so later changes to permissions, levels or user types never change what the migration did. Standard library only;
every set below is a copy of the live definition taken when the module was written (PERMISSION_LEVELS in
bootstrap/permissions.py, USER_TYPES in auth/user_types.py). Never import application code here and never edit
the copies; a different conversion is a new module.

Old model: user_roles (archived roles give nothing), users.post_id (home post, read and write), user_post_access
(post_id, can_write: read, or read and write), admin.true (the gate of the administration routes, and the record
exception: every post and user record, global statistics) and post.access_all (every post, read and write).
New model: role assignments, user + role + node ("root" or "p:<post id>"; the conversion never uses groups).

Rules (docs/superpowers/specs/2026-10-11-authorization-v2-design.md, section 8), for user U with roles R:

1. U holds the built-in admin role (admin_role_id, not archived): type admin, one assignment admin @ root. Done.
2. P(R) = permissions of R without REMOVED. When U does not hold admin.true, DEAD_WITHOUT_ADMIN are dead today
   and are left out.
3. U holds post.access_all: every P(R) at the root.
4. Otherwise, per R: root part = the system permissions of P(R), plus ADMIN_RECORD of P(R) when U holds
   admin.true, plus GLOBAL_VIEW of P(R) when U has neither a home post nor access rows (objects without a post
   are held anywhere: a dictionary reader without a post keeps the dictionaries); post part = the rest, at the
   home post and at every access row with can_write; view part = the *.view permissions of the post part, at
   every access row without can_write. A permission of the root part is never repeated in the other parts.
5. Each non-empty (set, node) goes to R itself when the set is R's stored set (permissions - REMOVED), otherwise
   to a derived role: one per distinct (set, archived flag) in the whole plan, named after the first source role
   <name>_system / _posts / _view (root / post / view part) with display name "<display name> (система)" /
   " (пости)" / " (перегляд)", unique (case-insensitive, like the MySQL collation) against taken_role_names and
   every derived name with _2, _3 ...; derived roles of an archived role are archived.
6. Type: the first of TYPE_ORDER whose ceiling covers every permission of U's assignments (archived roles
   included, so restoring a role never exceeds the type); with post.access_all, analyst rather than viewer.
7. A user whose plan has no assignment gets none (DIFF_NO_PERMISSIONS).

Open points decided here:
- U's flags (admin.true, post.access_all) come from the roles that are not archived (what applies today); archived
  roles are converted with the same flags, so an archived role never revives a dead permission.
- An access row on the home post adds nothing (the home post already gets the post part).
- dropped = every permission of U's roles (REMOVED aside) that no planned assignment carries: dead without
  admin.true, or scoped permissions of a user without any post.
- Permissions this module does not know (rows left in the database) stay in the roles but never decide the type.
- differences: what the new model does differently for U, see _differences.
"""
from dataclasses import dataclass
from types import MappingProxyType
from typing import Dict, FrozenSet, Iterable, Mapping, Optional, Set, Tuple, Union

ADMIN_TRUE = "admin.true"
ACCESS_ALL = "post.access_all"
REMOVED: FrozenSet[str] = frozenset({ADMIN_TRUE, ACCESS_ALL})

# The system level (assignable only at the root), admin.true included
SYSTEM: FrozenSet[str] = frozenset({
    "role.create", "role.update", "role.archive", "role.delete",
    "category.create", "category.update", "category.archive",
    "signal_type.create", "signal_type.update", "signal_type.archive",
    "tag.create", "tag.update", "tag.archive",
    "media_format.create", "media_format.update", "media_format.archive",
    "scope_group.create", "scope_group.update", "scope_group.archive",
    "media.monitor", "audit.view", ADMIN_TRUE,
})

# Objects without a post: held anywhere
GLOBAL_VIEW: FrozenSet[str] = frozenset({
    "role.view", "category.view", "signal_type.view", "tag.view", "media_format.view",
})

# The record exception of admin.true (every post and user record) and its global statistics
ADMIN_RECORD: FrozenSet[str] = frozenset({"post.view", "user.view", "statistics.view"})

# Every permission whose state-changing routes carry the admin gate today: without admin.true it does nothing
# (read-side uses such as include_deleted are lost with it, the safe direction)
DEAD_WITHOUT_ADMIN: FrozenSet[str] = frozenset({
    "user.create", "user.update", "user.archive",
    "post.create", "post.update", "post.archive",
    "role.view", "role.create", "role.update", "role.archive", "role.delete",
    "category.create", "category.update", "category.archive",
    "signal_type.create", "signal_type.update", "signal_type.archive",
    "tag.create", "tag.update", "tag.archive",
    "media.update",
    "media_format.create", "media_format.update", "media_format.archive",
    "export.create", "audit.view",
    "scope_group.view", "scope_group.create", "scope_group.update", "scope_group.archive",
    "media.monitor",
})

# User types: the order of the catalog and the ceilings
_EVERY_PERMISSION: FrozenSet[str] = frozenset({
    "user.view", "user.create", "user.update", "user.archive",
    "post.view", "post.create", "post.update", "post.archive",
    "observation.view", "observation.create", "observation.update", "observation.archive",
    "role.view", "role.create", "role.update", "role.archive", "role.delete",
    ADMIN_TRUE, "audit.view", "export.create",
    "category.view", "category.create", "category.update", "category.archive",
    "signal_type.view", "signal_type.create", "signal_type.update", "signal_type.archive",
    "tag.view", "tag.create", "tag.update", "tag.archive",
    "scope_group.view", "scope_group.create", "scope_group.update", "scope_group.archive",
    "media.view", "media.create", "media.update", "media.archive",
    ACCESS_ALL, "observation.manage_any", "media.manage_any",
    "media_format.view", "media_format.create", "media_format.update", "media_format.archive",
    "statistics.view", "media.monitor",
})
_VIEW = frozenset({"post.view", "observation.view", "media.view", "category.view", "signal_type.view", "tag.view"})
_AGGREGATES = frozenset({"statistics.view", "export.create"})
_OPERATOR_BASE = _VIEW | {
    "observation.create", "observation.update", "observation.archive", "media.create", "media.archive",
    "media_format.view",
}
_OVERRIDES = frozenset({"observation.manage_any", "media.manage_any"})
_TAG_EDIT = frozenset({"tag.create", "tag.update"})
_SENIOR_BASE = _OPERATOR_BASE | _OVERRIDES | _AGGREGATES
_POST_ADMIN_BASE = _SENIOR_BASE | {
    "media.update", "post.create", "post.update", "post.archive",
    "user.view", "user.create", "user.update", "user.archive", "role.view", "scope_group.view",
}

TYPE_ORDER: Tuple[str, ...] = ("viewer", "operator", "senior_operator", "analyst", "post_admin", "admin")
TYPE_CEILINGS: Mapping[str, FrozenSet[str]] = MappingProxyType({
    "viewer": _VIEW | _AGGREGATES,
    "operator": _OPERATOR_BASE | _OVERRIDES | _AGGREGATES | _TAG_EDIT,
    "senior_operator": _SENIOR_BASE | {"media.update", "user.view"} | _TAG_EDIT,
    "analyst": _VIEW | _AGGREGATES | {"user.view", "scope_group.view"},
    "post_admin": _POST_ADMIN_BASE | {
        "tag.create", "tag.update", "category.create", "category.update", "signal_type.create", "signal_type.update",
    },
    "admin": _EVERY_PERMISSION,
})

# What the new model does differently, reported per user (spec section 8 "Differences", plus the ones found
# while writing this module: administration, export, storage monitoring)
DIFF_UPLOAD_LIST = "upload_list_follows_manage_any"
DIFF_COUNTERS = "observation_counters_follow_view"
DIFF_ARCHIVED_RECORDS = "archived_records_need_archive"
DIFF_ARCHIVED_POST_DATA = "archived_post_observations_need_post_archive"
DIFF_NO_PERMISSIONS = "no_permissions"
DIFF_ADMINISTRATION = "administration_follows_scope"
DIFF_EXPORT_READ_ROWS = "export_needs_export_create_at_post"
DIFF_MONITOR = "storage_monitoring_needs_media_monitor"

# Gated routes that act on any post or user today, whatever the data scope
_ADMINISTRATION: FrozenSet[str] = frozenset({
    "post.create", "post.update", "post.archive", "user.create", "user.update", "user.archive", "scope_group.view",
})
# include_deleted shows archived rows of every post of the data scope today
_ARCHIVE_DATA: FrozenSet[str] = frozenset({"observation.archive", "media.archive", "post.archive", "user.archive"})

ROOT_KEY = "root"
ROLE_NAME_MAX = 100           # roles.name
ROLE_DISPLAY_NAME_MAX = 255   # roles.display_name

_ROOT_PART, _POST_PART, _VIEW_PART = "system", "posts", "view"
_DISPLAY_SUFFIX = {_ROOT_PART: " (система)", _POST_PART: " (пости)", _VIEW_PART: " (перегляд)"}


def post_key(post_id: int) -> str:
    return f"p:{post_id}"


@dataclass(frozen=True)
class LegacyRole:
    id: int
    name: str
    display_name: str
    is_deleted: bool
    permissions: FrozenSet[str]


@dataclass(frozen=True)
class LegacyUser:
    id: int
    username: str
    post_id: Optional[int]
    role_ids: Tuple[int, ...]
    access: Tuple[Tuple[int, bool], ...]   # user_post_access: (post_id, can_write)


RoleRef = Union[int, str]   # an existing role id, or the name of a derived role of the plan


@dataclass(frozen=True)
class PlannedAssignment:
    role: RoleRef
    node: str   # "root" or "p:<post id>"


@dataclass(frozen=True)
class DerivedRole:
    name: str
    display_name: str
    source_role_id: int   # the first role it was derived from
    permissions: FrozenSet[str]
    is_deleted: bool


@dataclass(frozen=True)
class UserPlan:
    user_id: int
    user_type: str
    assignments: Tuple[PlannedAssignment, ...]
    dropped: FrozenSet[str]
    differences: Tuple[str, ...]


@dataclass(frozen=True)
class ConversionPlan:
    users: Tuple[UserPlan, ...]
    derived_roles: Tuple[DerivedRole, ...]


@dataclass(frozen=True)
class _Flags:
    """What U holds through roles that are not archived"""
    admin: bool
    access_all: bool


@dataclass(frozen=True)
class _Places:
    """Where U's data scope is today: the home post, access rows with and without can_write (home post aside)"""
    home: Optional[int]
    write: Tuple[int, ...]
    read: Tuple[int, ...]
    has_posts: bool   # a home post or any access row

    @property
    def post_part_nodes(self) -> Tuple[str, ...]:
        homes = () if self.home is None else (self.home,)
        return tuple(post_key(post_id) for post_id in homes + self.write)

    @property
    def view_part_nodes(self) -> Tuple[str, ...]:
        return tuple(post_key(post_id) for post_id in self.read)


class _DerivedRoles:
    """The derived roles of one plan: one per distinct (permission set, archived flag), names unique"""

    def __init__(self, taken_role_names: Iterable[str]):
        self._taken: Set[str] = {name.casefold() for name in taken_role_names}
        self._by_set: Dict[Tuple[FrozenSet[str], bool], DerivedRole] = {}

    def role_for(self, source: LegacyRole, part: str, permissions: FrozenSet[str]) -> RoleRef:
        if permissions == source.permissions - REMOVED:
            return source.id
        key = (permissions, source.is_deleted)
        role = self._by_set.get(key)
        if role is None:
            suffix = _DISPLAY_SUFFIX[part]
            role = DerivedRole(
                name=self._unique_name(source.name, part),
                display_name=source.display_name[:ROLE_DISPLAY_NAME_MAX - len(suffix)] + suffix,
                source_role_id=source.id,
                permissions=permissions,
                is_deleted=source.is_deleted,
            )
            self._by_set[key] = role
        return role.name

    def _unique_name(self, base: str, part: str) -> str:
        number = 1
        while True:
            suffix = f"_{part}" if number == 1 else f"_{part}_{number}"
            name = base[:ROLE_NAME_MAX - len(suffix)] + suffix
            if name.casefold() not in self._taken:
                self._taken.add(name.casefold())
                return name
            number += 1

    def all(self) -> Tuple[DerivedRole, ...]:
        return tuple(self._by_set.values())


def plan_conversion(
        users: Iterable[LegacyUser],
        roles: Mapping[int, LegacyRole],
        admin_role_id: Optional[int],
        taken_role_names: Iterable[str],
) -> ConversionPlan:
    """The plan for every user, in user id order (deterministic: the same input always gives the same names)"""
    derived = _DerivedRoles(taken_role_names)
    plans = tuple(_plan_user(user, roles, admin_role_id, derived) for user in sorted(users, key=lambda u: u.id))
    return ConversionPlan(users=plans, derived_roles=derived.all())


def _plan_user(user: LegacyUser, roles: Mapping[int, LegacyRole], admin_role_id: Optional[int],
               derived: _DerivedRoles) -> UserPlan:
    own = tuple(roles[role_id] for role_id in sorted(set(user.role_ids)) if role_id in roles)
    held = frozenset().union(*(role.permissions - REMOVED for role in own))
    admin_role = roles.get(admin_role_id) if admin_role_id is not None else None
    if admin_role is not None and not admin_role.is_deleted and any(role.id == admin_role.id for role in own):
        return UserPlan(user.id, "admin", (PlannedAssignment(admin_role.id, ROOT_KEY),),
                        held - (admin_role.permissions - REMOVED), ())

    active = [role for role in own if not role.is_deleted]
    flags = _Flags(admin=any(ADMIN_TRUE in role.permissions for role in active),
                   access_all=any(ACCESS_ALL in role.permissions for role in active))
    places = _places(user)
    assignments: Dict[PlannedAssignment, None] = {}   # ordered set
    carried: Set[str] = set()
    alive_held: Set[str] = set()   # P(R) of the roles that are not archived: what applies today
    root_held: Set[str] = set()    # what those roles put at the root
    for role in own:
        alive = _alive(role, flags)
        parts = _split(alive, flags, places)
        for part, permissions in parts.items():
            nodes = _nodes(part, places)
            if not permissions or not nodes:
                continue
            ref = derived.role_for(role, part, permissions)
            carried |= permissions
            for node in nodes:
                assignments.setdefault(PlannedAssignment(ref, node))
        if not role.is_deleted:
            alive_held |= alive
            root_held |= parts[_ROOT_PART]

    return UserPlan(
        user_id=user.id,
        user_type=_user_type(frozenset(carried), flags.access_all),
        assignments=tuple(assignments),
        dropped=held - carried,
        differences=_differences(flags, places, frozenset(alive_held), frozenset(root_held), bool(assignments)),
    )


def _places(user: LegacyUser) -> _Places:
    rows: Dict[int, bool] = {}
    for post_id, can_write in user.access:
        rows[post_id] = rows.get(post_id, False) or bool(can_write)
    has_posts = user.post_id is not None or bool(rows)
    rows.pop(user.post_id, None)
    return _Places(
        home=user.post_id,
        write=tuple(sorted(post_id for post_id, can_write in rows.items() if can_write)),
        read=tuple(sorted(post_id for post_id, can_write in rows.items() if not can_write)),
        has_posts=has_posts,
    )


def _alive(role: LegacyRole, flags: _Flags) -> FrozenSet[str]:
    """P(R): REMOVED left out, and the dead permissions when U does not hold admin.true (rule 2)"""
    alive = role.permissions - REMOVED
    return alive if flags.admin else alive - DEAD_WITHOUT_ADMIN


def _split(alive: FrozenSet[str], flags: _Flags, places: _Places) -> Dict[str, FrozenSet[str]]:
    """The root, post and view parts of one role (rules 3 and 4)"""
    if flags.access_all:
        return {_ROOT_PART: alive, _POST_PART: frozenset(), _VIEW_PART: frozenset()}
    root = alive & SYSTEM
    if flags.admin:
        root |= alive & ADMIN_RECORD
    if not places.has_posts:
        root |= alive & GLOBAL_VIEW
    post = alive - root
    view = frozenset(name for name in post if name.endswith(".view"))
    return {_ROOT_PART: root, _POST_PART: post, _VIEW_PART: view}


def _nodes(part: str, places: _Places) -> Tuple[str, ...]:
    if part == _ROOT_PART:
        return (ROOT_KEY,)
    if part == _POST_PART:
        return places.post_part_nodes
    return places.view_part_nodes


def _user_type(permissions: FrozenSet[str], access_all: bool) -> str:
    """Rule 6. Permissions no ceiling knows (left in the database) never decide the type."""
    known = permissions & TYPE_CEILINGS["admin"]
    for key in TYPE_ORDER:
        if known <= TYPE_CEILINGS[key]:
            if key == "viewer" and access_all and known <= TYPE_CEILINGS["analyst"]:
                return "analyst"
            return key
    return TYPE_ORDER[-1]


def _differences(flags: _Flags, places: _Places, alive: FrozenSet[str], root: FrozenSet[str],
                 has_assignments: bool) -> Tuple[str, ...]:
    """What U could do today that the plan does not reproduce (alive: what applied today; root: what the plan puts
    at the root). Today an administrator (admin.true) acts on every post and user record and the read-only access
    rows count in every list; after the conversion everything follows the node where the permission is held."""
    scoped = not flags.access_all
    admin_scoped = flags.admin and scoped
    read_rows = bool(places.read)
    checks = (
        # GET /media/uploads with media.manage_any: every post for admin.true, the read-only rows too for everyone
        (DIFF_UPLOAD_LIST, scoped and "media.manage_any" in alive and (flags.admin or read_rows)),
        # GET /posts/{id}/observation-stats: any post for admin.true
        (DIFF_COUNTERS, admin_scoped and {"post.view", "observation.view"} <= alive),
        # archived post and user records: visible to admin.true without X.archive; include_deleted: archived rows of
        # the read-only rows too
        (DIFF_ARCHIVED_RECORDS, (flags.admin and (("post.view" in alive and "post.archive" not in root)
                                                  or ("user.view" in alive and "user.archive" not in root)))
         or (scoped and read_rows and bool(alive & _ARCHIVE_DATA))),
        # observation archive / restore in archived posts: admin.true + post.access_all, now post.archive at the post
        (DIFF_ARCHIVED_POST_DATA, flags.admin and flags.access_all
         and {"observation.view", "observation.archive"} <= alive and "post.archive" not in alive),
        # post and user administration acted on any post or user, the group list showed every group
        (DIFF_ADMINISTRATION, admin_scoped and bool(alive & _ADMINISTRATION)),
        # the export covered the read-only rows; export.create is not a view permission, it stays in the post part
        (DIFF_EXPORT_READ_ROWS, admin_scoped and read_rows and {"observation.view", "export.create"} <= alive),
        # storage statistics and poster status needed media.view behind the admin gate, now media.monitor
        (DIFF_MONITOR, flags.admin and "media.view" in alive and "media.monitor" not in alive),
        (DIFF_NO_PERMISSIONS, not has_assignments),
    )
    return tuple(sorted(tag for tag, differs in checks if differs))
