from typing import Optional
from fastapi import UploadFile
from pydantic import ConfigDict, Field, field_validator

from app.backend.enums.general import UserType
from app.backend.utils.validators import SecureBaseModel, SecurityValidators


class UserCreateData(SecureBaseModel):
    """Pydantic model for user creation validation"""
    username: str = Field(..., min_length=3, max_length=50)
    username_alias: str = Field(..., min_length=1, max_length=255)
    post_id: Optional[int] = Field(default=None, gt=0, description="Home post: who manages the user; null for none")
    user_type: UserType = Field(description="The type of the user (GET /user-types): its base role is assigned at the "
                                            "home post, at the root without one or when the base role holds system "
                                            "permissions")

    @field_validator('username')
    @classmethod
    def validate_username(cls, v: str) -> str:
        return SecurityValidators.validate_username(v)

    @field_validator('username_alias')
    @classmethod
    def validate_username_alias(cls, v: str) -> str:
        return SecurityValidators.sanitize_string(v, max_length=255)


class UserUpdateData(SecureBaseModel):
    """Pydantic model for user update validation - all fields optional"""
    username: Optional[str] = Field(default=None, min_length=3, max_length=50)
    username_alias: Optional[str] = Field(default=None, min_length=1, max_length=255)
    post_id: Optional[int] = Field(default=None, gt=0)
    is_active: Optional[bool] = Field(default=None)

    @field_validator('username')
    @classmethod
    def validate_username(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            return SecurityValidators.validate_username(v)
        return v

    @field_validator('username_alias')
    @classmethod
    def validate_username_alias(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            return SecurityValidators.sanitize_string(v, max_length=255)
        return v


class UserCreateForm(UserCreateData):
    """Form of POST /users/ (multipart/form-data). Unknown form fields are ignored, as with plain Form() params."""
    model_config = ConfigDict(extra="ignore")

    certificate: UploadFile = Field(description="PKI certificate file (PEM or DER format)")


class UserUpdateForm(UserUpdateData):
    """Form of PATCH /users/{user_id} (multipart/form-data or urlencoded): only the fields sent are changed.
    Unknown form fields (is_deleted included) are ignored, as with plain Form() params."""
    model_config = ConfigDict(extra="ignore")

    certificate: Optional[UploadFile] = Field(default=None, description="New PKI certificate file (PEM or DER format)")
