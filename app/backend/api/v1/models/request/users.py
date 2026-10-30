from typing import Optional
from fastapi import UploadFile
from pydantic import ConfigDict, Field, field_validator

from app.backend.enums.errors import FieldErrorCode
from app.backend.enums.general import UserType
from app.backend.errors.coded import invalid
from app.backend.pki.profile import LABEL_MAX_LENGTH, normalize_label
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
        return SecurityValidators.plain_text(v, max_length=255)


class UserUpdateData(SecureBaseModel):
    """Pydantic model for user update validation - all fields optional"""
    username: Optional[str] = Field(default=None, min_length=3, max_length=50)
    username_alias: Optional[str] = Field(default=None, min_length=1, max_length=255)
    post_id: Optional[int] = Field(default=None, gt=0)
    is_active: Optional[bool] = Field(default=None)
    user_type: Optional[UserType] = Field(
        default=None,
        description="Another type (GET /user-types): every assignment of the old type's built-in role gets the new "
                    "type's built-in role at the same node (at the root when it holds system permissions), every other "
                    "assignment must fit the new ceiling, and the user's sessions end")

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
            return SecurityValidators.plain_text(v, max_length=255)
        return v


class UserCreateForm(UserCreateData):
    """Form of POST /users/ (multipart/form-data). Unknown form fields are ignored, as with plain Form() params."""
    model_config = ConfigDict(extra="ignore")

    certificate: Optional[UploadFile] = Field(
        default=None,
        description="Certificate file (PEM or DER) the user logs in with, registered as an uploaded certificate; "
                    "without one the user cannot log in until a certificate is issued or uploaded")
    certificate_label: Optional[str] = Field(
        default=None,
        description=f"Readable name of the certificate, 1 to {LABEL_MAX_LENGTH} characters after trimming, no control "
                    f"characters (default: its CN cut to {LABEL_MAX_LENGTH} characters); only used with certificate")

    @field_validator('certificate_label')
    @classmethod
    def validate_certificate_label(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        label = normalize_label(v)
        if label is None:
            raise invalid(FieldErrorCode.CERTIFICATE_LABEL_INVALID)
        return label


class UserUpdateForm(UserUpdateData):
    """Form of PATCH /users/{user_id} (multipart/form-data or urlencoded): only the fields sent are changed.
    Unknown form fields (is_deleted and certificate included) are ignored, as with plain Form() params: certificates
    are issued or uploaded for the user and revoked on their own."""
    model_config = ConfigDict(extra="ignore")
