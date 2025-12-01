from typing import Optional
from pydantic import BaseModel, Field, field_validator
import re


class LoginRequest(BaseModel):
    certificate_cn: str = Field(..., min_length=1, max_length=255)
    certificate_serial: str = Field(..., min_length=1, max_length=255)
    certificate_fingerprint: str = Field(..., min_length=1, max_length=512)

    @field_validator('certificate_cn')
    @classmethod
    def validate_cn(cls, v: str) -> str:
        # Allow alphanumeric, spaces, dots, dashes, underscores, equals, and @ symbols for email-like CNs
        if not re.match(r'^[a-zA-Z0-9\s\-_.=@]+$', v):
            raise ValueError('Invalid certificate CN format')
        return v.strip()

    @field_validator('certificate_serial')
    @classmethod
    def validate_serial(cls, v: str) -> str:
        # Allow alphanumeric characters, dashes and underscores for serials
        if not re.match(r'^[a-fA-F0-9\-_]+$', v):
            raise ValueError('Invalid certificate serial format')
        return v.strip()

    @field_validator('certificate_fingerprint')
    @classmethod
    def validate_fingerprint(cls, v: str) -> str:
        # Allow hex characters, colons, and dashes for fingerprints
        if not re.match(r'^[a-fA-F0-9:\-]+$', v):
            raise ValueError('Invalid certificate fingerprint format')
        return v.strip()


class RefreshRequest(BaseModel):
    refresh_token: str = Field(..., min_length=1)


class LogoutRequest(BaseModel):
    session_id: Optional[str] = None
