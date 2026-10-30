from typing import Optional

from pydantic import BaseModel, Field, ValidationInfo, field_validator

from app.backend.utils.validators import SecurityValidators
from app.backend.errors.coded import invalid
from app.backend.enums.errors import FieldErrorCode


def _required_text(value: str, max_length: int) -> str:
    """A required single-line plain text (SecurityValidators.plain_text); spaces alone would store an empty value, so
    they are rejected"""
    value = SecurityValidators.plain_text(value, max_length=max_length)
    if not value:
        raise invalid(FieldErrorCode.VALUE_EMPTY)
    return value


def _optional_text(value: Optional[str], field: str) -> Optional[str]:
    """description: multi-line plain text up to 1000 characters; region and city: single-line up to 255"""
    if value is None:
        return value
    if field == "description":
        return SecurityValidators.plain_text(value, max_length=1000, multiline=True)
    return SecurityValidators.plain_text(value, max_length=255)


class PostCreate(BaseModel):
    """Create post with proper validation matching DB constraints"""

    name: str = Field(min_length=1, max_length=255, description="Post name")
    code: str = Field(min_length=1, max_length=50, description="Post code (unique)")
    description: Optional[str] = Field(None, max_length=1000, description="Post description")
    region: Optional[str] = Field(None, max_length=255, description="Post region")
    city: Optional[str] = Field(None, max_length=255, description="Post city")
    group_id: Optional[int] = Field(default=None, gt=0, description="Scope group the post sits in; null: directly under the root")

    @field_validator('name', 'code')
    @classmethod
    def validate_required_fields(cls, v: str) -> str:
        """Validate and sanitize required string fields"""
        return _required_text(v, max_length=255)

    @field_validator('description', 'region', 'city')
    @classmethod
    def validate_optional_fields(cls, v: Optional[str], info: ValidationInfo) -> Optional[str]:
        """Plain text: description multi-line, region and city single-line"""
        return _optional_text(v, info.field_name)


class PostUpdate(BaseModel):
    """Update post with proper validation matching DB constraints"""

    name: Optional[str] = Field(None, min_length=1, max_length=255, description="Post name")
    code: Optional[str] = Field(None, min_length=1, max_length=50, description="Post code (unique)")
    description: Optional[str] = Field(None, max_length=1000, description="Post description")
    region: Optional[str] = Field(None, max_length=255, description="Post region")
    city: Optional[str] = Field(None, max_length=255, description="Post city")
    is_active: Optional[bool] = None
    group_id: Optional[int] = Field(default=None, gt=0, description="Scope group the post sits in; null: directly under the root")
    force: bool = Field(default=False, description="With a move: run it at once even with uploads running below the node; they continue at the new place. Without uploads, and without a move, it changes nothing")

    # Validators do not run on the default, so None here is an explicit null for a NOT NULL column
    @field_validator('name', 'code')
    @classmethod
    def validate_name(cls, v: Optional[str]) -> str:
        """Validate and sanitize name and code fields; omit the field to keep the current value"""
        if v is None:
            raise invalid(FieldErrorCode.VALUE_NULL)
        return _required_text(v, max_length=255)

    @field_validator('is_active')
    @classmethod
    def validate_is_active(cls, v: Optional[bool]) -> bool:
        if v is None:
            raise invalid(FieldErrorCode.VALUE_NULL)
        return v

    @field_validator('description', 'region', 'city')
    @classmethod
    def validate_optional_fields(cls, v: Optional[str], info: ValidationInfo) -> Optional[str]:
        """Plain text: description multi-line, region and city single-line"""
        return _optional_text(v, info.field_name)
