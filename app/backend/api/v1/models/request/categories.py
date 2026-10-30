from typing import Optional
from pydantic import Field, field_validator
from app.backend.utils.validators import SecureBaseModel, SecurityValidators, not_null


class CategoryCreate(SecureBaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    alias: str = Field(..., min_length=1, max_length=255)

    @field_validator('name', 'alias')
    @classmethod
    def validate_fields(cls, v: str) -> str:
        return SecurityValidators.plain_text(v, max_length=255)


class CategoryUpdate(SecureBaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=255)
    alias: Optional[str] = Field(None, min_length=1, max_length=255)

    refuse_null = not_null('name', 'alias')

    @field_validator('name', 'alias')
    @classmethod
    def validate_fields(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            return SecurityValidators.plain_text(v, max_length=255)
        return v
