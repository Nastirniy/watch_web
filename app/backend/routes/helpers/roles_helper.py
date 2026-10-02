from typing import Optional
from fastapi import status
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select

from app.backend.models.role import Role, Permission
from app.backend.services.api_errors import ApiError
from app.backend.enums.errors import ErrorCode


async def validate_role_id(role_id: int) -> None:
    """Validate role ID is positive"""
    if role_id <= 0:
        raise ApiError(ErrorCode.ROLE_ID_INVALID)


async def validate_permission_id(permission_id: int) -> None:
    """Validate permission ID is positive"""
    if permission_id <= 0:
        raise ApiError(ErrorCode.PERMISSION_ID_INVALID)


async def get_role_or_404(session: AsyncSession, role_id: int, include_deleted: bool = False) -> Role:
    """Get role by ID or raise 404. Soft-deleted roles are 404 unless include_deleted is set."""
    await validate_role_id(role_id)
    result = await session.exec(select(Role).where(Role.id == role_id))
    role = result.first()
    if role and role.is_deleted and not include_deleted:
        role = None
    if not role:
        raise ApiError(ErrorCode.ROLE_NOT_FOUND)
    return role


async def get_permission_or_404(session: AsyncSession, permission_id: int) -> Permission:
    """Get permission by ID or raise 404"""
    await validate_permission_id(permission_id)
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