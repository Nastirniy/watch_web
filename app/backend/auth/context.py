"""Per-request authorization context.

Loaded once per request by dependencies.get_auth_context (three queries: the user's role assignments with their roles
and permissions, every group, every post). Every permission and scope check reads from this object; nothing queries
the database again during the request.

Access is the user's role assignments (role_assignments: tree role + node of the scope tree) and system role
assignments (system_role_assignments: system role, no node), each a Grant: its permissions apply to the node and
everything below it (auth/scope.py); a system assignment is a grant at the root of kind system, so every check treats
it as held at the root. Archived roles grant nothing, and a grant on an
archived post or at an archived group applies nowhere (it is kept: it applies again after a restore), so it does not
count as held anywhere either.
"""
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Iterable, Optional, Set, Tuple, Union

from loguru import logger
from sqlalchemy import null as sa_null
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.auth.integrity import effective_permissions
from app.backend.auth.scope import Grant, Node, ROOT, ScopeTree
from app.backend.enums.general import Permissions
from app.backend.models.user import User
from app.backend.models.post import Post
from app.backend.models.role import Role, Permission, RolePermission
from app.backend.models.role_assignment import RoleAssignment, node_of
from app.backend.models.system_role_assignment import SystemRoleAssignment
from app.backend.models.scope_group import ScopeGroup


def permission_name(permission: Union[Permissions, str]) -> str:
    """The name of a permission given as a Permissions member or a plain string (the one helper of auth/)"""
    return permission.value if isinstance(permission, Permissions) else str(permission)


@dataclass(frozen=True)
class AuthContext:
    user: User
    grants: Tuple[Grant, ...] = ()      # what the user holds where: one per role assignment of a role not archived
    tree: ScopeTree = field(default_factory=ScopeTree.empty)
    session_id: Optional[str] = None    # user_sessions.id of the access token (audit, logout)
    # every permission of the grants in effect, computed once (require() reads it on every request)
    _held_anywhere: FrozenSet[str] = field(init=False, repr=False, compare=False, default=frozenset())

    def __post_init__(self):
        held = frozenset().union(*(grant.permissions for grant in self.grants if not self.tree.is_archived(grant.node)))
        object.__setattr__(self, "_held_anywhere", held)

    @property
    def permissions(self) -> FrozenSet[str]:
        """Every permission held at some node (held_anywhere): the menu of the web client, role edits"""
        return self._held_anywhere

    # ------------------------------------------------------------------ grants on the scope tree
    @property
    def held_anywhere(self) -> FrozenSet[str]:
        """Every permission held at some node in effect (the root, a group or a post that is not archived): route
        reachability, objects without a post, the web menu. A grant on an archived post or at an archived group
        applies nowhere and is left out."""
        return self._held_anywhere

    def has_anywhere(self, permission: Union[Permissions, str]) -> bool:
        return permission_name(permission) in self.held_anywhere

    def has_at_root(self, permission: Union[Permissions, str]) -> bool:
        name = permission_name(permission)
        return any(grant.node == ROOT and name in grant.permissions for grant in self.grants)

    def posts_with(self, permission: Union[Permissions, str]) -> Optional[FrozenSet[int]]:
        """Posts where the permission is in effect. None means every post (held at the root); otherwise every post
        below the non-archived groups holding it plus the non-archived posts holding it directly."""
        name = permission_name(permission)
        posts: Set[int] = set()
        for grant in self.grants:
            if name in grant.permissions:
                reach = self.tree.posts_in_effect(grant.node)
                if reach is None:
                    return None
                posts |= reach
        return frozenset(posts)

    def has_at_post(self, permission: Union[Permissions, str], post_id: int) -> bool:
        """The permission is in effect at the post: held at the root, at a non-archived group above it, or at the
        post itself when it is not archived"""
        name = permission_name(permission)
        in_effect = set(self.tree.nodes_in_effect_at_post(post_id))
        return any(name in grant.permissions and grant.node in in_effect for grant in self.grants)

    def permissions_at_post(self, post_id: int) -> FrozenSet[str]:
        """Every permission in effect at the post (has_at_post of each)"""
        return self._held_at(post_id, self.grants)

    def _held_at(self, post_id: int, grants: Iterable[Grant]) -> FrozenSet[str]:
        in_effect = set(self.tree.nodes_in_effect_at_post(post_id))
        return frozenset().union(*(grant.permissions for grant in grants if grant.node in in_effect))

    @property
    def system_permissions(self) -> FrozenSet[str]:
        """Permissions of the system grants (system role assignments)"""
        return frozenset().union(*(grant.permissions for grant in self.grants if grant.kind == "system"))

    @property
    def root_permissions(self) -> FrozenSet[str]:
        """Permissions held at the root: in effect at every post"""
        return frozenset().union(*(grant.permissions for grant in self.grants if grant.node == ROOT))

    def post_permissions(self) -> Dict[int, FrozenSet[str]]:
        """post id -> the permissions the grants below the root (groups, posts) put in effect at that post, for every
        post they reach. In effect at a post = root_permissions plus its entry (permissions_at_post): a grant on an
        archived post stops applying there, a group's grants keep applying to its archived posts."""
        below_root = [grant for grant in self.grants if grant.node != ROOT and grant.permissions]
        reached: Set[int] = set()
        for grant in below_root:
            reached |= self.tree.posts_in_effect(grant.node)
        entries = {post_id: self._held_at(post_id, below_root) for post_id in sorted(reached)}
        return {post_id: held for post_id, held in entries.items() if held}

    def group_permissions(self) -> Dict[int, FrozenSet[str]]:
        """group id -> the permissions the grants at groups put in effect at that group: the grant's group and every
        group below it, archived descendants included (like covers), for every group they reach. In effect at a group
        = root_permissions plus its entry. A grant at an archived group reaches nothing; grants at the root and at
        posts are left out, and so are empty entries."""
        at_groups = [grant for grant in self.grants
                     if grant.node.kind == "group" and grant.permissions and not self.tree.is_archived(grant.node)]
        entries: Dict[int, FrozenSet[str]] = {}
        for group_id in sorted(self.tree.group_parent):
            node = Node.group(group_id)
            held = frozenset().union(*(grant.permissions for grant in at_groups if self.tree.contains(grant.node, node)))
            if held:
                entries[group_id] = held
        return entries

    def covers(self, permission: Union[Permissions, str], node: Node) -> bool:
        """The node lies inside a subtree where the permission is held (delegation). A grant at an archived node
        covers nothing."""
        name = permission_name(permission)
        return any(
            name in grant.permissions and not self.tree.is_archived(grant.node) and self.tree.contains(grant.node, node)
            for grant in self.grants
        )

    def groups_with(self, permission: Union[Permissions, str]) -> Optional[FrozenSet[int]]:
        """Groups inside a subtree where the permission is held (covers), archived ones included. None means every
        group (held at the root)."""
        if self.has_at_root(permission):
            return None
        return frozenset(group_id for group_id in self.tree.group_parent if self.covers(permission, Node.group(group_id)))

    def covers_user(self, permission: Union[Permissions, str], user: User) -> bool:
        """The permission is in effect at the user's home post, or at the root for a user without one"""
        if user.post_id is None:
            return self.has_at_root(permission)
        return self.has_at_post(permission, user.post_id)


def grant_rows(user_id: int, include_archived_roles: bool = False):
    """The one definition of what a user holds: every role assignment of a role that is not archived, one row per
    permission of the role (permission None for a role without any): (assignment id, group id, post id, role id,
    name, display name, builtin_key, permission name). include_archived_roles reads every existing assignment
    (privileges.check_target_user: what the user would hold again after a role's restore); never for an AuthContext"""
    query = (
        select(RoleAssignment.id, RoleAssignment.scope_group_id, RoleAssignment.scope_post_id,
               Role.id, Role.name, Role.display_name, Role.builtin_key, Permission.name)
        .select_from(RoleAssignment)
        .join(Role, Role.id == RoleAssignment.role_id)
        .outerjoin(RolePermission, RolePermission.role_id == Role.id)
        .outerjoin(Permission, Permission.id == RolePermission.permission_id)
        .where(RoleAssignment.user_id == user_id)
    )
    return query if include_archived_roles else query.where(Role.is_deleted == False)


def system_grant_rows(user_id: int, include_archived_roles: bool = False):
    """grant_rows of the user's system role assignments, in the same shape (both scope ids null: the root)"""
    query = (
        select(SystemRoleAssignment.id, sa_null(), sa_null(),
               Role.id, Role.name, Role.display_name, Role.builtin_key, Permission.name)
        .select_from(SystemRoleAssignment)
        .join(Role, Role.id == SystemRoleAssignment.role_id)
        .outerjoin(RolePermission, RolePermission.role_id == Role.id)
        .outerjoin(Permission, Permission.id == RolePermission.permission_id)
        .where(SystemRoleAssignment.user_id == user_id)
    )
    return query if include_archived_roles else query.where(Role.is_deleted == False)


# (kind, assignment id) of the grants whose left-out permissions were logged by this process: one line each
_WARNED: Set[Tuple[str, int]] = set()


def _in_effect(user: User, kind: str, assignment_id: int, names: FrozenSet[str]) -> FrozenSet[str]:
    """What the grant puts in effect for the user (auth/integrity.py); a grant with something left out is logged
    once per process (ids and permission names, nothing about the user)"""
    kept, left_out = effective_permissions(user.user_type, kind, names)
    if left_out and (kind, assignment_id) not in _WARNED:
        _WARNED.add((kind, assignment_id))
        logger.warning(f"Grant {kind}:{assignment_id} of user {user.id}: {sorted(left_out)} not in effect "
                       f"(level or boundary of type {user.user_type})")
    return kept


def grants_of(rows: Iterable, kind: str = "tree", user: Optional[User] = None) -> Tuple[Grant, ...]:
    """The grants of grant_rows (kind tree) or system_grant_rows (kind system), in assignment id order. With user,
    each grant holds only what it puts in effect for that user: the permissions of the kind's level inside the
    boundary of the user's type (auth/integrity.py)."""
    heads: Dict[int, tuple] = {}            # assignment id -> role id, name, display name, built-in, node
    permissions: Dict[int, Set[str]] = {}
    for assignment_id, group_id, post_id, role_id, name, display_name, builtin_key, permission in rows:
        heads.setdefault(assignment_id, (role_id, name, display_name, builtin_key is not None, node_of(group_id, post_id)))
        held = permissions.setdefault(assignment_id, set())
        if permission:
            held.add(permission)
    def held_by(assignment_id: int) -> FrozenSet[str]:
        names = frozenset(permissions[assignment_id])
        return names if user is None else _in_effect(user, kind, assignment_id, names)

    return tuple(Grant(*heads[assignment_id], held_by(assignment_id), assignment_id=assignment_id, kind=kind)
                 for assignment_id in sorted(heads))


async def load_grants(session: AsyncSession, user: User, include_archived_roles: bool = False) -> Tuple[Grant, ...]:
    """What a user holds where (two queries): the tree grants, then the system grants, each with what it puts in
    effect for the user (the level of its kind, the boundary of the user's type)"""
    tree = grants_of((await session.exec(grant_rows(user.id, include_archived_roles))).all(), user=user)
    system = grants_of((await session.exec(system_grant_rows(user.id, include_archived_roles))).all(), kind="system",
                       user=user)
    return tree + system


async def load_scope_tree(session: AsyncSession) -> ScopeTree:
    """Two queries: every group and every post, archived ones included"""
    groups = (await session.exec(
        select(ScopeGroup.id, ScopeGroup.parent_id, ScopeGroup.is_deleted, ScopeGroup.kind))).all()
    posts = (await session.exec(select(Post.id, Post.group_id, Post.is_deleted))).all()
    return ScopeTree(
        group_parent={group_id: parent_id for group_id, parent_id, _, _ in groups},
        archived_groups=frozenset(group_id for group_id, _, is_deleted, _ in groups if is_deleted),
        post_group={post_id: group_id for post_id, group_id, _ in posts},
        archived_posts=frozenset(post_id for post_id, _, is_deleted in posts if is_deleted),
        group_kind={group_id: kind for group_id, _, _, kind in groups},
    )


async def load_auth_context(session: AsyncSession, user: User, session_id: Optional[str] = None) -> AuthContext:
    """Three queries: the grants, the scope tree (groups, posts)"""
    grants = await load_grants(session, user)
    return AuthContext(user=user, grants=grants, tree=await load_scope_tree(session), session_id=session_id)
