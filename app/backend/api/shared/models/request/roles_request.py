from typing import List, Optional
from pydantic import Field, field_validator

from app.backend.utils.validators import SecureBaseModel, SecurityValidators
from app.backend.services.api_errors import invalid
from app.backend.enums.errors import FieldErrorCode


class RoleCreate(SecureBaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    display_name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = Field(default=None, max_length=500)
    permission_ids: Optional[List[int]] = Field(default=None)

    @field_validator('name')
    @classmethod
    def validate_name(cls, v):
        return SecurityValidators.sanitize_string(v, max_length=100)

    @field_validator('display_name')
    @classmethod
    def validate_display_name(cls, v):
        return SecurityValidators.sanitize_string(v, max_length=255)

    @field_validator('description')
    @classmethod
    def validate_description(cls, v):
        if v is not None:
            return SecurityValidators.sanitize_string(v, max_length=500)
        return v

    @field_validator('permission_ids')
    @classmethod
    def validate_permission_ids(cls, v):
        if v is not None:
            if not isinstance(v, list):
                raise invalid(FieldErrorCode.PERMISSION_IDS_NOT_LIST)
            if any(pid <= 0 for pid in v):
                raise invalid(FieldErrorCode.PERMISSION_IDS_INVALID)
        return v


class RoleUpdate(SecureBaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    display_name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = Field(default=None, max_length=500)
    permission_ids: Optional[List[int]] = Field(default=None)

    @field_validator('name')
    @classmethod
    def validate_name(cls, v):
        if v is not None:
            return SecurityValidators.sanitize_string(v, max_length=100)
        return v

    @field_validator('display_name')
    @classmethod
    def validate_display_name(cls, v):
        if v is not None:
            return SecurityValidators.sanitize_string(v, max_length=255)
        return v

    @field_validator('description')
    @classmethod
    def validate_description(cls, v):
        if v is not None:
            return SecurityValidators.sanitize_string(v, max_length=500)
        return v

    @field_validator('permission_ids')
    @classmethod
    def validate_permission_ids(cls, v):
        if v is not None:
            if not isinstance(v, list):
                raise invalid(FieldErrorCode.PERMISSION_IDS_NOT_LIST)
            if any(pid <= 0 for pid in v):
                raise invalid(FieldErrorCode.PERMISSION_IDS_INVALID)
        return v
