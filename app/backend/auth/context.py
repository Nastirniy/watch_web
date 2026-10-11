"""Per-request authorization context.

Loaded once per request by dependencies.get_auth_context. Every permission,
admin and post scope check reads from this object; nothing queries the
database again during the request.
"""
from dataclasses import dataclass
from typing import Dict, FrozenSet, List, Optional, Set, Tuple, Union

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.enums.general import Permissions
from app.backend.models.user import User
from app.backend.models.role import Role, Permission, RolePermission, UserRole
from app.backend.models.user_post_access import UserPostAccess


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
    readable_post_ids: FrozenSet[int]   # home post + every user_post_access row
    writable_post_ids: FrozenSet[int]   # home post + user_post_access rows with can_write
    session_id: Optional[str] = None    # user_sessions.id of the access token (audit, logout)

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
        name = permission.value if isinstance(permission, Permissions) else str(permission)
        return name in self.permissions

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
    """What a user holds: (role id, name, display name, permission name or None) for every role of the user that is
    not archived"""
    return held_permissions(Role.id, Role.name, Role.display_name, Permission.name).where(UserRole.user_id == user_id)


async def effective_permissions(session: AsyncSession, user_id: int) -> FrozenSet[str]:
    """Permission names a user holds through roles that are not archived (AuthContext.permissions of that user)"""
    rows = (await session.exec(role_permission_rows(user_id))).all()
    return frozenset(permission_name for *_, permission_name in rows if permission_name)


async def load_auth_context(session: AsyncSession, user: User, session_id: Optional[str] = None) -> AuthContext:
    """Two queries: roles with their permissions, and extra post access rows."""
    role_rows = (await session.exec(role_permission_rows(user.id))).all()

    roles: Dict[int, RoleBrief] = {}
    permissions: Set[str] = set()
    for role_id, role_name, role_display_name, permission_name in role_rows:
        roles.setdefault(role_id, RoleBrief(id=role_id, name=role_name, display_name=role_display_name))
        if permission_name:
            permissions.add(permission_name)

    access_rows = (await session.exec(
        select(UserPostAccess.post_id, UserPostAccess.can_write)
        .where(UserPostAccess.user_id == user.id)
    )).all()

    readable: Set[int] = set()
    writable: Set[int] = set()
    if user.post_id is not None:
        readable.add(user.post_id)
        writable.add(user.post_id)
    for post_id, can_write in access_rows:
        readable.add(post_id)
        if can_write:
            writable.add(post_id)

    return AuthContext(
        user=user,
        permissions=frozenset(permissions),
        roles=tuple(sorted(roles.values(), key=lambda role: role.id)),
        readable_post_ids=frozenset(readable),
        writable_post_ids=frozenset(writable),
        session_id=session_id,
    )
