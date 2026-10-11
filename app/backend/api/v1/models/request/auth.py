from typing import List
from pydantic import BaseModel, Field, field_validator
import re

from app.backend.enums.general import Permissions
from app.backend.errors.coded import invalid
from app.backend.enums.errors import FieldErrorCode


class LoginRequest(BaseModel):
    certificate_cn: str = Field(..., min_length=1, max_length=255)
    certificate_serial: str = Field(..., min_length=1, max_length=255)
    certificate_fingerprint: str = Field(..., min_length=1, max_length=512)

    @field_validator('certificate_cn')
    @classmethod
    def validate_cn(cls, v: str) -> str:
        # Allow alphanumeric, spaces, dots, dashes, underscores, equals, and @ symbols for email-like CNs
        if not re.match(r'^[a-zA-Z0-9\s\-_.=@]+$', v):
            raise invalid(FieldErrorCode.CERTIFICATE_CN_INVALID)
        return v.strip()

    @field_validator('certificate_serial')
    @classmethod
    def validate_serial(cls, v: str) -> str:
        # Allow alphanumeric characters, dashes and underscores for serials
        if not re.match(r'^[a-fA-F0-9\-_]+$', v):
            raise invalid(FieldErrorCode.CERTIFICATE_SERIAL_INVALID)
        return v.strip()

    @field_validator('certificate_fingerprint')
    @classmethod
    def validate_fingerprint(cls, v: str) -> str:
        # Allow hex characters, colons, and dashes for fingerprints
        if not re.match(r'^[a-fA-F0-9:\-]+$', v):
            raise invalid(FieldErrorCode.CERTIFICATE_FINGERPRINT_INVALID)
        return v.strip()


class RefreshRequest(BaseModel):
    refresh_token: str = Field(..., min_length=1)


class PermissionCheckRequest(BaseModel):
    """Same shape as a route requirement: all AND any AND (admin.true if admin)"""
    all: List[str] = Field(default_factory=list)
    any: List[str] = Field(default_factory=list)
    admin: bool = False

    @field_validator('all', 'any')
    @classmethod
    def validate_known_permissions(cls, v: List[str]) -> List[str]:
        known = {p.value for p in Permissions}
        unknown = [name for name in v if name not in known]
        if unknown:
            raise invalid(FieldErrorCode.PERMISSIONS_UNKNOWN, permissions=sorted(unknown))
        return v
