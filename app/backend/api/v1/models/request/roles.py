from typing import List, Optional
from pydantic import Field, field_validator, PositiveInt

from app.backend.enums.errors import FieldErrorCode
from app.backend.enums.general import RoleKind
from app.backend.errors.coded import invalid
from app.backend.utils.validators import SecureBaseModel, SecurityValidators


class RoleClone(SecureBaseModel):
    """Name and texts of a custom copy of a role (POST /roles/{role_id}/clone); the permissions are copied"""
    name: str = Field(..., min_length=1, max_length=100)
    display_name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = Field(default=None, max_length=500)

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


class RoleCreate(RoleClone):
    kind: RoleKind = Field(description="tree: holds tree permissions, assigned at a node (/users/{user_id}/assignments); "
                                        "system: holds system permissions, assigned without a node "
                                        "(/users/{user_id}/system-assignments). Never changes.")
    permission_ids: Optional[List[PositiveInt]] = Field(default=None)

    @field_validator('permission_ids')
    @classmethod
    def validate_permission_ids(cls, v: Optional[List[int]]) -> Optional[List[int]]:
        if v is not None and len(v) != len(set(v)):
            raise invalid(FieldErrorCode.PERMISSION_IDS_DUPLICATE)
        return v


class RoleUpdate(SecureBaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=100)
    display_name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = Field(default=None, max_length=500)
    permission_ids: Optional[List[PositiveInt]] = Field(default=None)

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
    def validate_permission_ids(cls, v: Optional[List[int]]) -> Optional[List[int]]:
        if v is not None and len(v) != len(set(v)):
            raise invalid(FieldErrorCode.PERMISSION_IDS_DUPLICATE)
        return v
