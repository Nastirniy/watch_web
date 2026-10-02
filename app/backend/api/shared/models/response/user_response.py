from typing import Optional
from datetime import datetime

from app.backend.utils.validators import SecureBaseModel


class UserResponse(SecureBaseModel):
    id: int
    username: str
    username_alias: str
    post_id: Optional[int]
    is_active: bool
    created_at: datetime
    updated_at: Optional[datetime]
    last_login_at: Optional[datetime]
    is_online: bool = False
    last_activity_at: Optional[datetime] = None
    is_deleted: bool = False


class UserRoleResponse(SecureBaseModel):
    """Response model for roles assigned to users (includes assignment timestamp)"""
    id: int
    name: str
    display_name: str
    assigned_at: datetime


class UserPublicResponse(SecureBaseModel):
    """Public projection of a user, used as nested creator info (observation.user, media.user)"""
    id: int
    username: str
    username_alias: str
    is_online: bool = False
    last_activity_at: Optional[datetime] = None
    is_deleted: bool = False


class UserPostAccessResponse(SecureBaseModel):
    id: int
    user_id: int
    post_id: int
    can_write: bool
    created_at: datetime
