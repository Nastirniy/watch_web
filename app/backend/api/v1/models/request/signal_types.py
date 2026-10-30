from typing import Optional
from pydantic import Field, field_validator
from app.backend.utils.validators import SecureBaseModel, SecurityValidators, not_null


class SignalTypeCreate(SecureBaseModel):
    code: str = Field(..., min_length=1, max_length=50)
    name: str = Field(..., min_length=1, max_length=255)
    category_id: Optional[int] = Field(None, gt=0)
    description: Optional[str] = Field(None, max_length=1000)
    is_active: bool = Field(True)
    sort_order: int = Field(0, ge=0)

    @field_validator('code', 'name')
    @classmethod
    def validate_required_fields(cls, v: str) -> str:
        return SecurityValidators.plain_text(v, max_length=255)

    @field_validator('description')
    @classmethod
    def validate_description(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            return SecurityValidators.plain_text(v, max_length=1000, multiline=True)
        return v


class SignalTypeUpdate(SecureBaseModel):
    code: Optional[str] = Field(None, min_length=1, max_length=50)
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    category_id: Optional[int] = Field(None, gt=0)
    description: Optional[str] = Field(None, max_length=1000)
    is_active: Optional[bool] = None
    sort_order: Optional[int] = Field(None, ge=0)

    refuse_null = not_null('code', 'name', 'is_active', 'sort_order')

    @field_validator('code')
    @classmethod
    def validate_code(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            return SecurityValidators.plain_text(v, max_length=50)
        return v

    @field_validator('name')
    @classmethod
    def validate_name(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            return SecurityValidators.plain_text(v, max_length=255)
        return v

    @field_validator('description')
    @classmethod
    def validate_description(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            return SecurityValidators.plain_text(v, max_length=1000, multiline=True)
        return v
