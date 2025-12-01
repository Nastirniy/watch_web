from typing import List, Optional
from pydantic import Field, field_validator

from app.backend.utils.validators import SecureBaseModel, SecurityValidators


class UserCreateData(SecureBaseModel):
    """Pydantic model for user creation validation"""
    username: str = Field(..., min_length=3, max_length=50)
    username_alias: str = Field(..., min_length=1, max_length=255)
    post_id: Optional[int] = Field(default=None, gt=0)
    role_ids: List[int] = Field(..., min_items=1, description="List of role IDs to assign to the user")

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
        if len(v) == 0:
            raise ValueError('At least one role must be assigned to the user')
        if len(v) != len(set(v)):
            raise ValueError('Duplicate role IDs are not allowed')
        for role_id in v:
            if role_id <= 0:
                raise ValueError('Role IDs must be positive integers')
        return v


class UserUpdateData(SecureBaseModel):
    """Pydantic model for user update validation - all fields optional"""
    username_alias: Optional[str] = Field(default=None, min_length=1, max_length=255)
    post_id: Optional[int] = Field(default=None, gt=0)
    is_active: Optional[bool] = Field(default=None)
    role_ids: Optional[List[int]] = Field(default=None, min_items=1, description="List of role IDs to replace current roles")

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
            if len(v) == 0:
                raise ValueError('If providing role_ids, at least one role must be specified')
            if len(v) != len(set(v)):
                raise ValueError('Duplicate role IDs are not allowed')
            for role_id in v:
                if role_id <= 0:
                    raise ValueError('Role IDs must be positive integers')
        return v
