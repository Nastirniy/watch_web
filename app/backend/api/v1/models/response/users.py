from typing import Optional

from pydantic import Field

from app.backend.enums.general import UserType
from app.backend.utils.validators import ResponseDatetime, SecureBaseModel


class UserResponse(SecureBaseModel):
    id: int
    username: str
    username_alias: str
    post_id: Optional[int]
    user_type: UserType = Field(description="The type of the user (GET /user-types): the pages of the web client")
    is_active: bool
    created_at: ResponseDatetime
    updated_at: Optional[ResponseDatetime]
    last_login_at: Optional[ResponseDatetime]
    is_online: bool = False
    last_activity_at: Optional[ResponseDatetime] = None
    is_deleted: bool = False


class UserPublicResponse(SecureBaseModel):
    """Public projection of a user, used as nested creator info (observation.user, media.user)"""
    id: int
    username: str
    username_alias: str
    is_online: bool = False
    last_activity_at: Optional[ResponseDatetime] = None
    is_deleted: bool = False
