from typing import Optional
from pydantic import Field, field_validator
from app.backend.utils.validators import SecureBaseModel, SecurityValidators, not_null
from app.backend.errors.coded import invalid
from app.backend.enums.errors import FieldErrorCode


def _clean_name(value: str) -> str:
    value = SecurityValidators.sanitize_string(value.strip(), max_length=255)
    if not value:
        raise invalid(FieldErrorCode.VALUE_EMPTY)
    return value


def _clean_description(value: Optional[str]) -> Optional[str]:
    return SecurityValidators.sanitize_string(value.strip(), max_length=1000) if value is not None else value


class ScopeGroupCreate(SecureBaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    parent_id: Optional[int] = Field(default=None, gt=0, description="Group it sits in; null: directly under the root")
    description: Optional[str] = Field(default=None, max_length=1000)

    @field_validator('name')
    @classmethod
    def validate_name(cls, v: str) -> str:
        return _clean_name(v)

    @field_validator('description')
    @classmethod
    def validate_description(cls, v: Optional[str]) -> Optional[str]:
        return _clean_description(v)


class ScopeGroupUpdate(SecureBaseModel):
    """Only the fields sent are changed; parent_id null moves the group directly under the root"""
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    parent_id: Optional[int] = Field(default=None, gt=0, description="Move to this group; null: directly under the root")
    description: Optional[str] = Field(default=None, max_length=1000)

    refuse_null = not_null('name')

    @field_validator('name')
    @classmethod
    def validate_name(cls, v: Optional[str]) -> Optional[str]:
        return _clean_name(v) if v is not None else v

    @field_validator('description')
    @classmethod
    def validate_description(cls, v: Optional[str]) -> Optional[str]:
        return _clean_description(v)
