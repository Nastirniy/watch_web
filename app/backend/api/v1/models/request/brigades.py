from typing import Optional
from pydantic import Field, field_validator

from app.backend.api.v1.models.request.scope_groups import _clean_description, _clean_name
from app.backend.utils.validators import SecureBaseModel, not_null


class BrigadeCreate(SecureBaseModel):
    """A brigade sits directly inside a corps"""
    corps_id: int = Field(..., gt=0, description="Corps the brigade sits in")
    name: str = Field(..., min_length=1, max_length=255)
    description: Optional[str] = Field(default=None, max_length=1000)

    @field_validator('name')
    @classmethod
    def validate_name(cls, v: str) -> str:
        return _clean_name(v)

    @field_validator('description')
    @classmethod
    def validate_description(cls, v: Optional[str]) -> Optional[str]:
        return _clean_description(v)


class BrigadeUpdate(SecureBaseModel):
    """Only the fields sent are changed; corps_id moves the brigade, with its posts, to another corps"""
    corps_id: Optional[int] = Field(default=None, gt=0, description="Move to this corps")
    name: Optional[str] = Field(default=None, min_length=1, max_length=255)
    description: Optional[str] = Field(default=None, max_length=1000)

    refuse_null = not_null('corps_id', 'name')

    @field_validator('name')
    @classmethod
    def validate_name(cls, v: Optional[str]) -> Optional[str]:
        return _clean_name(v) if v is not None else v

    @field_validator('description')
    @classmethod
    def validate_description(cls, v: Optional[str]) -> Optional[str]:
        return _clean_description(v)
