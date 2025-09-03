from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from pydantic import BaseModel
from datetime import datetime

from app.backend.database import get_session
from app.backend.models.user import User
from app.backend.dependencies import get_current_user
from app.backend.services.permissions import PermissionService
from app.backend.enums import Routes, Permissions

router = APIRouter()


class UserCreate(BaseModel):
    username: str
    username_alias: str
    pki_certificate_cn: str
    pki_certificate_serial: str
    pki_certificate_fingerprint: str
    post_id: Optional[int] = None


class UserUpdate(BaseModel):
    username_alias: Optional[str] = None
    post_id: Optional[int] = None
    is_active: Optional[bool] = None


@router.get(Routes.USERS_LIST.value.replace("/users", ""), response_model=List[User])
@PermissionService.require_permission(Permissions.USER_VIEW.value)
async def list_users(
        skip: int = 0,
        limit: int = 100,
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
    """List all users"""
    result = await session.exec(
        select(User)
        .where(User.is_deleted == False)
        .offset(skip)
        .limit(limit)
    )
    return result.all()


@router.post(Routes.USERS_CREATE.value.replace("/users", ""), response_model=User)
@PermissionService.require_permission(Permissions.USER_CREATE.value)
async def create_user(
        user_data: UserCreate,
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
    """Create new user"""
    # Check if user exists
    result = await session.exec(
        select(User).where(User.username == user_data.username)
    )
    if result.first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User with this username already exists"
        )

    user = User(**user_data.dict())
    session.add(user)
    await session.commit()
    await session.refresh(user)

    return user


@router.get(Routes.USERS_GET.value.replace("/users/", ""), response_model=User)
@PermissionService.require_permission(Permissions.USER_VIEW.value)
async def get_user(
        user_id: int,
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
    """Get user by ID"""
    result = await session.exec(
        select(User).where(User.id == user_id, User.is_deleted == False)
    )
    user = result.first()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    return user


@router.patch(Routes.USERS_UPDATE.value.replace("/users/", ""), response_model=User)
@PermissionService.require_permission(Permissions.USER_UPDATE.value)
async def update_user(
        user_id: int,
        user_update: UserUpdate,
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
    """Update user"""
    result = await session.exec(
        select(User).where(User.id == user_id, User.is_deleted == False)
    )
    user = result.first()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    for field, value in user_update.dict(exclude_unset=True).items():
        setattr(user, field, value)

    user.updated_at = datetime.utcnow()
    session.add(user)
    await session.commit()
    await session.refresh(user)

    return user