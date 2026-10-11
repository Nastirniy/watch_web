"""Per-request authorization context.

Loaded once per request by dependencies.get_auth_context. Every permission,
admin and post scope check reads from this object; nothing queries the
database again during the request.

Two views of the same access live here during the move to authorization v2:
- the legacy fields (permissions, roles, readable_post_ids, writable_post_ids and the methods on them), which
  every route reads today, with their old semantics;
- grants on the scope tree (grants, tree, held_anywhere, has_at_post, posts_with, covers, ...), computed from the
  same old tables through the frozen conversion (legacy_grants.py) until role assignments replace them.
"""
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, List, Optional, Set, Tuple, Union

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.auth.legacy_grants import legacy_grants
from app.backend.auth.scope import Grant, Node, ROOT, ScopeTree
from app.backend.enums.general import Permissions, UserType
from app.backend.models.user import User
from app.backend.models.post import Post
from app.backend.models.role import Role, Permission, RolePermission, UserRole
from app.backend.models.scope_group import ScopeGroup
from app.backend.models.user_post_access import UserPostAccess


def _permission_name(permission: Union[Permissions, str]) -> str:
    return permission.value if isinstance(permission, Permissions) else str(permission)


@dataclass(frozen=True)
class RoleBrief:
    id: int
    name: str
    display_name: str


@dataclass(frozen=True)
class AuthContext:
    user: User
    permissions: FrozenSet[str]
    roles: Tuple[RoleBrief, ...]
    readable_post_ids: FrozenSet[int]   # home post + every user_post_access row, archived posts left out
    writable_post_ids: FrozenSet[int]   # home post + user_post_access rows with can_write, archived posts left out
    session_id: Optional[str] = None    # user_sessions.id of the access token (audit, logout)
    grants: Tuple[Grant, ...] = ()      # what the user holds where (authorization v2)
    tree: ScopeTree = field(default_factory=ScopeTree.empty)

    @property
    def is_admin(self) -> bool:
        """Holds admin.true: may open the administration area (gate only, no wildcard)."""
        return Permissions.IS_ADMIN.value in self.permissions

    @property
    def has_post_access_all(self) -> bool:
        return Permissions.POST_ACCESS_ALL.value in self.permissions

    @property
    def role_ids(self) -> FrozenSet[int]:
        return frozenset(role.id for role in self.roles)

    def has(self, permission: Union[Permissions, str]) -> bool:
        return _permission_name(permission) in self.permissions

    def can_view_post(self, post_id: int) -> bool:
        return self.has_post_access_all or post_id in self.readable_post_ids

    def can_write_post(self, post_id: int) -> bool:
        return self.has_post_access_all or post_id in self.writable_post_ids

    def post_scope(self) -> Optional[FrozenSet[int]]:
        """Post ids the user may read. None means unrestricted (post.access_all)."""
        return None if self.has_post_access_all else self.readable_post_ids

    def accessible_post_ids_api(self) -> Optional[List[int]]:
        """Value exposed to API clients. None means all posts."""
        return None if self.has_post_access_all else sorted(self.readable_post_ids)

    # ------------------------------------------------------------------ grants on the scope tree
    @property
    def held_anywhere(self) -> FrozenSet[str]:
        """Every permission held at some node: route reachability, objects without a post, the web menu"""
        return frozenset().union(*(grant.permissions for grant in self.grants))

    def has_anywhere(self, permission: Union[Permissions, str]) -> bool:
        return _permission_name(permission) in self.held_anywhere

    def has_at_root(self, permission: Union[Permissions, str]) -> bool:
        name = _permission_name(permission)
        return any(grant.node == ROOT and name in grant.permissions for grant in self.grants)

    def posts_with(self, permission: Union[Permissions, str]) -> Optional[FrozenSet[int]]:
        """Posts where the permission is in effect. None means every post (held at the root); otherwise every post
        below the non-archived groups holding it plus the non-archived posts holding it directly."""
        name = _permission_name(permission)
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
        name = _permission_name(permission)
        in_effect = set(self.tree.nodes_in_effect_at_post(post_id))
        return any(name in grant.permissions and grant.node in in_effect for grant in self.grants)

    def covers(self, permission: Union[Permissions, str], node: Node) -> bool:
        """The node lies inside a subtree where the permission is held (delegation). A grant at an archived node
        covers nothing."""
        name = _permission_name(permission)
        return any(
            name in grant.permissions and not self.tree.is_archived(grant.node) and self.tree.contains(grant.node, node)
            for grant in self.grants
        )

    def covers_user(self, permission: Union[Permissions, str], user: User) -> bool:
        """The permission is in effect at the user's home post, or at the root for a user without one"""
        if user.post_id is None:
            return self.has_at_root(permission)
        return self.has_at_post(permission, user.post_id)


def held_permissions(*columns):
    """The one definition of what users hold: select(*columns) over every role assignment (UserRole) of a role that
    is not archived, one row per permission of the role (Permission columns None for a role without any).
    load_auth_context, effective_permissions and privileges.py add their own columns and filters."""
    return (
        select(*columns)
        .select_from(UserRole)
        .join(Role, Role.id == UserRole.role_id)
        .outerjoin(RolePermission, RolePermission.role_id == Role.id)
        .outerjoin(Permission, Permission.id == RolePermission.permission_id)
        .where(Role.is_deleted == False)
    )


def role_permission_rows(user_id: int):
    """What a user holds: (role id, name, display name, builtin_key, permission name or None) for every role of the
    user that is not archived"""
    return held_permissions(Role.id, Role.name, Role.display_name, Role.builtin_key, Permission.name).where(
        UserRole.user_id == user_id)


async def effective_permissions(session: AsyncSession, user_id: int) -> FrozenSet[str]:
    """Permission names a user holds through roles that are not archived (AuthContext.permissions of that user)"""
    rows = (await session.exec(role_permission_rows(user_id))).all()
    return frozenset(permission_name for *_, permission_name in rows if permission_name)


def access_rows(user_id: int):
    """(post id, can_write) of every user_post_access row of the user, archived posts included"""
    return select(UserPostAccess.post_id, UserPostAccess.can_write).where(UserPostAccess.user_id == user_id)


async def load_scope_tree(session: AsyncSession) -> ScopeTree:
    """Two queries: every group and every post, archived ones included"""
    groups = (await session.exec(select(ScopeGroup.id, ScopeGroup.parent_id, ScopeGroup.is_deleted))).all()
    posts = (await session.exec(select(Post.id, Post.group_id, Post.is_deleted))).all()
    return ScopeTree(
        group_parent={group_id: parent_id for group_id, parent_id, _ in groups},
        archived_groups=frozenset(group_id for group_id, _, is_deleted in groups if is_deleted),
        post_group={post_id: group_id for post_id, group_id, _ in posts},
        archived_posts=frozenset(post_id for post_id, _, is_deleted in posts if is_deleted),
    )


def _legacy_data_scope(user: User, rows: List[Tuple[int, bool]], tree: ScopeTree) -> Tuple[FrozenSet[int], FrozenSet[int]]:
    """readable and writable post ids: the home post (read and write) and every access row (write with can_write).
    An archived post is in no home-post or access-row scope (only post.access_all still reads its data); the rows
    stay and apply again after a restore. A post that does not exist is in no scope."""
    readable: Set[int] = set()
    writable: Set[int] = set()
    for post_id, can_write in ([(user.post_id, True)] if user.post_id is not None else []) + list(rows):
        if post_id not in tree.post_group or post_id in tree.archived_posts:
            continue
        readable.add(post_id)
        if can_write:
            writable.add(post_id)
    return frozenset(readable), frozenset(writable)


async def load_auth_context(session: AsyncSession, user: User, session_id: Optional[str] = None) -> AuthContext:
    """Four queries: roles with their permissions, the access rows, the scope tree (groups, posts)."""
    role_rows = (await session.exec(role_permission_rows(user.id))).all()
    rows = [(post_id, bool(can_write)) for post_id, can_write in (await session.exec(access_rows(user.id))).all()]
    tree = await load_scope_tree(session)

    roles: Dict[int, RoleBrief] = {}
    permissions: Set[str] = set()
    admin_role_id: Optional[int] = None
    for role_id, role_name, role_display_name, builtin_key, permission_name in role_rows:
        roles.setdefault(role_id, RoleBrief(id=role_id, name=role_name, display_name=role_display_name))
        if builtin_key == UserType.ADMIN.value:
            admin_role_id = role_id
        if permission_name:
            permissions.add(permission_name)

    readable, writable = _legacy_data_scope(user, rows, tree)
    return AuthContext(
        user=user,
        permissions=frozenset(permissions),
        roles=tuple(sorted(roles.values(), key=lambda role: role.id)),
        readable_post_ids=readable,
        writable_post_ids=writable,
        session_id=session_id,
        grants=legacy_grants(user, role_rows, rows, admin_role_id),
        tree=tree,
    )
