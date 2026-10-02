from typing import List, Optional
from pydantic import BaseModel, Field, PositiveInt, field_validator

from app.backend.utils.validators import SecureBaseModel, SecurityValidators
from app.backend.services.api_errors import invalid
from app.backend.enums.errors import FieldErrorCode


class UserCreateData(SecureBaseModel):
    """Pydantic model for user creation validation"""
    username: str = Field(..., min_length=3, max_length=50)
    username_alias: str = Field(..., min_length=1, max_length=255)
    post_id: Optional[int] = Field(default=None, gt=0)
    role_ids: List[PositiveInt] = Field(..., min_length=1, description="List of role IDs to assign to the user")

    @field_validator('username')
    @classmethod
    def validate_username(cls, v: str) -> str:
        return SecurityValidators.validate_username(v)

    @field_validator('username_alias')
    @classmethod
    def validate_username_alias(cls, v: str) -> str:
        return SecurityValidators.sanitize_string(v, max_length=255)

    @field_validator('role_ids')
    @classmethod
    def validate_role_ids(cls, v: List[int]) -> List[int]:
        if len(v) != len(set(v)):
            raise invalid(FieldErrorCode.ROLE_IDS_DUPLICATE)
        return v


class UserUpdateData(SecureBaseModel):
    """Pydantic model for user update validation - all fields optional"""
    username: Optional[str] = Field(default=None, min_length=3, max_length=50)
    username_alias: Optional[str] = Field(default=None, min_length=1, max_length=255)
    post_id: Optional[int] = Field(default=None, gt=0)
    is_active: Optional[bool] = Field(default=None)
    role_ids: Optional[List[PositiveInt]] = Field(default=None, min_length=1, description="List of role IDs to replace current roles")

    @field_validator('username')
    @classmethod
    def validate_username(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            return SecurityValidators.validate_username(v)
        return v

    @field_validator('username_alias')
    @classmethod
    def validate_username_alias(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            return SecurityValidators.sanitize_string(v, max_length=255)
        return v

    @field_validator('role_ids')
    @classmethod
    def validate_role_ids(cls, v: Optional[List[int]]) -> Optional[List[int]]:
        if v is not None:
            if len(v) != len(set(v)):
                raise invalid(FieldErrorCode.ROLE_IDS_DUPLICATE)
        return v


class UserPostAccessUpsert(BaseModel):
    """Grant or update access of a user to an additional post"""
    can_write: bool = Field(default=False, description="Allow creating and updating data in that post")
