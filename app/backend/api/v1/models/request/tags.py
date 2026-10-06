from typing import Optional
from pydantic import Field, field_validator
from app.backend.utils.validators import SecureBaseModel, SecurityValidators


class TagCreate(SecureBaseModel):
    name: str = Field(..., min_length=1, max_length=100)

    @field_validator('name')
    @classmethod
    def validate_name(cls, v: str) -> str:
        return SecurityValidators.sanitize_string(v.strip().lower(), max_length=100)


class TagUpdate(SecureBaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)

    @field_validator('name')
    @classmethod
    def validate_name(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            return SecurityValidators.sanitize_string(v.strip().lower(), max_length=100)
        return v
