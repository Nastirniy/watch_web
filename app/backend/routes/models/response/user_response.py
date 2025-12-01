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


class UserRoleResponse(SecureBaseModel):
    """Response model for roles assigned to users (includes assignment timestamp)"""
    id: int
    name: str
    display_name: str
    assigned_at: datetime
