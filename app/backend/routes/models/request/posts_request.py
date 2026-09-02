from typing import Optional

from app.backend.enums.general import ConnectionStatus
from pydantic import BaseModel, Field, field_validator

from app.backend.utils.validators import SecurityValidators


class PostCreate(BaseModel):
    """Create post with proper validation matching DB constraints"""

    name: str = Field(min_length=1, max_length=255, description="Post name")
    code: str = Field(min_length=1, max_length=50, description="Post code (unique)")
    description: Optional[str] = Field(None, max_length=1000, description="Post description")
    region: Optional[str] = Field(None, max_length=255, description="Post region")
    city: Optional[str] = Field(None, max_length=255, description="Post city")

    @field_validator('name', 'code')
    @classmethod
    def validate_required_fields(cls, v: str) -> str:
        """Validate and sanitize required string fields"""
        return SecurityValidators.sanitize_string(v.strip(), max_length=255)

    @field_validator('description', 'region', 'city')
    @classmethod
    def validate_optional_fields(cls, v: Optional[str]) -> Optional[str]:
        """Validate and sanitize optional string fields"""
        if v is not None:
            return SecurityValidators.sanitize_string(v.strip(), max_length=1000)
        return v


class PostUpdate(BaseModel):
    """Update post with proper validation matching DB constraints"""

    name: Optional[str] = Field(None, min_length=1, max_length=255, description="Post name")
    code: Optional[str] = Field(None, min_length=1, max_length=50, description="Post code (unique)")
    description: Optional[str] = Field(None, max_length=1000, description="Post description")
    region: Optional[str] = Field(None, max_length=255, description="Post region")
    city: Optional[str] = Field(None, max_length=255, description="Post city")
    is_active: Optional[bool] = None
    connection_status: Optional[str] = Field(None, description="Manual status: offline, maintenance or error. 'online' is derived from user presence and cannot be stored.")

    @field_validator('connection_status')
    @classmethod
    def validate_connection_status(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        allowed = {ConnectionStatus.OFFLINE.value, ConnectionStatus.MAINTENANCE.value, ConnectionStatus.ERROR.value}
        if v not in allowed:
            raise ValueError(f"connection_status must be one of {sorted(allowed)}")
        return v

    @field_validator('name', 'code')
    @classmethod
    def validate_name(cls, v: Optional[str]) -> Optional[str]:
        """Validate and sanitize name and code fields"""
        if v is not None:
            return SecurityValidators.sanitize_string(v.strip(), max_length=255)
        return v

    @field_validator('description', 'region', 'city')
    @classmethod
    def validate_optional_fields(cls, v: Optional[str]) -> Optional[str]:
        """Validate and sanitize optional string fields"""
        if v is not None:
            return SecurityValidators.sanitize_string(v.strip(), max_length=1000)
        return v
