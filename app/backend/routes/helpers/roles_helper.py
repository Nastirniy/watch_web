from typing import Optional
from fastapi import HTTPException, status
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select

from app.backend.models.role import Role, Permission


async def validate_role_id(role_id: int) -> None:
    """Validate role ID is positive"""
    if role_id <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Role ID must be a positive integer"
        )


async def validate_permission_id(permission_id: int) -> None:
    """Validate permission ID is positive"""
    if permission_id <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Permission ID must be a positive integer"
        )


async def get_role_or_404(session: AsyncSession, role_id: int) -> Role:
    """Get role by ID or raise 404"""
    await validate_role_id(role_id)
    result = await session.exec(select(Role).where(Role.id == role_id))
    role = result.first()
    if not role:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Role not found"
        )
    return role


async def get_permission_or_404(session: AsyncSession, permission_id: int) -> Permission:
    """Get permission by ID or raise 404"""
    await validate_permission_id(permission_id)
    result = await session.exec(select(Permission).where(Permission.id == permission_id))
    permission = result.first()
    if not permission:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Permission not found"
        )
    return permission


async def check_role_name_exists(session: AsyncSession, name: str, exclude_id: Optional[int] = None) -> None:
    """Check if role name already exists"""
    query = select(Role).where(Role.name == name)
    if exclude_id:
        query = query.where(Role.id != exclude_id)
    result = await session.exec(query)
    if result.first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Role with this name already exists"
        )