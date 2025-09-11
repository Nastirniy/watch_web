from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlalchemy.exc import IntegrityError, DataError
from sqlmodel import select
from pydantic import BaseModel, Field, field_validator
from datetime import datetime, timezone

from app.backend.database import get_session
from app.backend.models.user import User
from app.backend.dependencies import get_current_user
from app.backend.services.permissions import PermissionService
from app.backend.enums.general import Permissions
from app.backend.enums.routes import UserRoutes, SystemRoutes
from app.backend.utils.validators import (
    SecureBaseModel, 
    SecurityValidators, 
    PaginationParams,
    create_secure_string_field,
    create_secure_id_field
)
from app.backend.utils.logging_config import get_logger

router = APIRouter()
logger = get_logger("users")


class UserCreate(SecureBaseModel):
    username: str = Field(..., min_length=3, max_length=50)
    username_alias: str = Field(..., min_length=1, max_length=255)
    pki_certificate_cn: str = Field(..., min_length=1, max_length=255)
    pki_certificate_serial: str = Field(..., min_length=1, max_length=255)
    pki_certificate_fingerprint: str = Field(..., min_length=1, max_length=512)
    post_id: Optional[int] = Field(default=None, gt=0)

    @field_validator('username')
    @classmethod
    def validate_username(cls, v):
        return SecurityValidators.validate_username(v)

    @field_validator('username_alias')
    @classmethod
    def validate_username_alias(cls, v):
        return SecurityValidators.sanitize_string(v, max_length=255)

    @field_validator('pki_certificate_cn')
    @classmethod
    def validate_cn(cls, v):
        return SecurityValidators.sanitize_string(v, max_length=255)

    @field_validator('pki_certificate_serial')
    @classmethod
    def validate_serial(cls, v):
        return SecurityValidators.sanitize_string(v, max_length=255)

    @field_validator('pki_certificate_fingerprint')
    @classmethod
    def validate_fingerprint(cls, v):
        return SecurityValidators.sanitize_string(v, max_length=512)


class UserUpdate(SecureBaseModel):
    username_alias: Optional[str] = Field(default=None, min_length=1, max_length=255)
    post_id: Optional[int] = Field(default=None, gt=0)
    is_active: Optional[bool] = None

    @field_validator('username_alias')
    @classmethod
    def validate_username_alias(cls, v):
        if v is not None:
            return SecurityValidators.sanitize_string(v, max_length=255)
        return v


class UserResponse(SecureBaseModel):
    id: int
    username: str
    username_alias: str
    post_id: Optional[int]
    is_active: bool
    created_at: datetime
    updated_at: Optional[datetime]
    last_login_at: Optional[datetime]


@router.get(SystemRoutes.ROOT.value, response_model=List[UserResponse])
@PermissionService.require_permission(Permissions.USER_VIEW.value)
async def list_users(
        pagination: PaginationParams = Depends(),
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
    """List all users with secure pagination"""
    try:
        skip = (pagination.page - 1) * pagination.size
        
        result = await session.exec(
            select(User)
            .where(User.is_deleted == False)
            .offset(skip)
            .limit(pagination.size)
            .order_by(User.created_at.desc())
        )
        
        users = result.all()
        logger.info(f"User {current_user.username} listed {len(users)} users")
        
        return [UserResponse(
            id=user.id,
            username=user.username,
            username_alias=user.username_alias,
            post_id=user.post_id,
            is_active=user.is_active,
            created_at=user.created_at,
            updated_at=user.updated_at,
            last_login_at=user.last_login_at
        ) for user in users]

    except Exception as e:
        logger.error(f"Error listing users: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve users"
        )


@router.post(SystemRoutes.ROOT.value, response_model=User)
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

    # Hash PKI certificate data before storing
    from app.backend.services.auth import AuthService
    user_dict = user_data.model_dump()
    user_dict['pki_certificate_cn_hash'] = AuthService.hash_pki_data(user_dict.pop('pki_certificate_cn'))
    user_dict['pki_certificate_serial_hash'] = AuthService.hash_pki_data(user_dict.pop('pki_certificate_serial'))
    user_dict['pki_certificate_fingerprint_hash'] = AuthService.hash_pki_data(user_dict.pop('pki_certificate_fingerprint'))
    
    user = User(**user_dict)
    session.add(user)
    
    try:
        await session.commit()
        await session.refresh(user)
        return user
    except (IntegrityError, DataError) as e:
        await session.rollback()
        error_msg = str(e)
        if "Data too long" in error_msg:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Data too long for one or more fields"
            )
        elif "Duplicate entry" in error_msg or "already exists" in error_msg:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="User with this username already exists"
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Database error occurred"
            )


@router.get(UserRoutes.USER_BY_ID.value, response_model=User)
@PermissionService.require_permission(Permissions.USER_VIEW.value)
async def get_user(
        user_id: int,
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
    """Get user by ID"""
    if user_id <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User ID must be a positive integer"
        )
    
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


@router.patch(UserRoutes.USER_BY_ID.value, response_model=User)
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

    for field, value in user_update.model_dump(exclude_unset=True).items():
        setattr(user, field, value)

    user.updated_at = datetime.now(timezone.utc)
    session.add(user)
    await session.commit()
    await session.refresh(user)

    return user
