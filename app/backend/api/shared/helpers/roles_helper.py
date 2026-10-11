from typing import Dict, List, Optional, Set
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select

from app.backend.models.role import Role, Permission, RolePermission
from app.backend.errors.problems import ApiError
from app.backend.enums.errors import ErrorCode


async def get_role_or_404(session: AsyncSession, role_id: int, include_deleted: bool = False) -> Role:
    """Get role by ID or raise 404. Soft-deleted roles are 404 unless include_deleted is set."""
    result = await session.exec(select(Role).where(Role.id == role_id))
    role = result.first()
    if role and role.is_deleted and not include_deleted:
        role = None
    if not role:
        raise ApiError(ErrorCode.ROLE_NOT_FOUND)
    return role


async def get_permission_or_404(session: AsyncSession, permission_id: int) -> Permission:
    """Get permission by ID or raise 404"""
    result = await session.exec(select(Permission).where(Permission.id == permission_id))
    permission = result.first()
    if not permission:
        raise ApiError(ErrorCode.PERMISSION_NOT_FOUND)
    return permission


async def check_role_name_exists(session: AsyncSession, name: str, exclude_id: Optional[int] = None) -> None:
    """Check if role name already exists"""
    query = select(Role).where(Role.name == name)
    if exclude_id:
        query = query.where(Role.id != exclude_id)
    result = await session.exec(query)
    existing = result.first()
    if existing:
        if existing.is_deleted:
            raise ApiError(ErrorCode.ROLE_NAME_ARCHIVED, role_id=existing.id)
        raise ApiError(ErrorCode.ROLE_NAME_TAKEN)


async def permission_names_by_role(session: AsyncSession, role_ids: List[int]) -> Dict[int, Set[str]]:
    """Permission names of every given role, in one query"""
    names: Dict[int, Set[str]] = {}
    if not role_ids:
        return names
    rows = await session.exec(
        select(RolePermission.role_id, Permission.name)
        .join(Permission, RolePermission.permission_id == Permission.id)
        .where(RolePermission.role_id.in_(role_ids))
    )
    for role_id, name in rows.all():
        names.setdefault(role_id, set()).add(name)
    return names
