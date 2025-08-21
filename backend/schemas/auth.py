"""
Authentication schemas for login, tokens, and PKI certificate handling.
Handles user authentication and session management.
"""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field, field_validator

from backend.schemas.base import BaseSchema, TimestampSchema


class PKICertificateInfo(BaseSchema):
    """PKI certificate information."""

    common_name: str = Field(description="Certificate Common Name (CN)")
    serial_number: str = Field(description="Certificate serial number")
    fingerprint: str = Field(description="Certificate fingerprint")
    issuer: Optional[str] = Field(None, description="Certificate issuer")
    subject: Optional[str] = Field(None, description="Certificate subject")
    valid_from: Optional[datetime] = Field(None, description="Certificate valid from date")
    valid_to: Optional[datetime] = Field(None, description="Certificate valid to date")

    @field_validator('common_name', 'serial_number', 'fingerprint')
    @classmethod
    def validate_required_fields(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("Field cannot be empty")
        return v.strip()


class LoginRequest(BaseSchema):
    """Login request with PKI certificate."""

    certificate: PKICertificateInfo = Field(description="PKI certificate information")
    username: Optional[str] = Field(None, description="Username (optional if using cert auth)")
    session_metadata: Optional[dict] = Field(None, description="Additional session metadata")

    @field_validator('certificate')
    @classmethod
    def validate_certificate(cls, v: PKICertificateInfo) -> PKICertificateInfo:
        # Additional certificate validation could be added here
        return v


class TokenPair(BaseSchema):
    """Access and refresh token pair."""

    access_token: str = Field(description="JWT access token")
    refresh_token: str = Field(description="JWT refresh token")
    token_type: str = Field(default="bearer", description="Token type")


class LoginResponse(BaseSchema):
    """Login response with tokens and user info."""

    tokens: TokenPair = Field(description="Authentication tokens")
    user: "UserInfo" = Field(description="User information")
    session_id: str = Field(description="Session ID")
    expires_at: datetime = Field(description="Token expiration time")
    permissions: List[str] = Field(default_factory=list, description="User permissions")


class RefreshTokenRequest(BaseSchema):
    """Refresh token request."""

    refresh_token: str = Field(description="Refresh token")


class RefreshTokenResponse(BaseSchema):
    """Refresh token response."""

    access_token: str = Field(description="New access token")
    token_type: str = Field(default="bearer", description="Token type")
    expires_at: datetime = Field(description="Token expiration time")


class LogoutRequest(BaseSchema):
    """Logout request."""

    session_id: Optional[str] = Field(None, description="Session ID to logout (optional)")
    logout_all: bool = Field(False, description="Logout from all sessions")


class PasswordChangeRequest(BaseSchema):
    """Password change request."""

    current_password: str = Field(description="Current password")
    new_password: str = Field(min_length=8, description="New password")
    confirm_password: str = Field(description="Password confirmation")

    @field_validator('confirm_password')
    @classmethod
    def passwords_match(cls, v: str, info) -> str:
        if 'new_password' in info.data and v != info.data['new_password']:
            raise ValueError('Passwords do not match')
        return v


class SessionInfo(TimestampSchema):
    """Session information."""

    id: str = Field(description="Session ID")
    ip_address: Optional[str] = Field(None, description="Client IP address")
    user_agent: Optional[str] = Field(None, description="Client user agent")
    last_activity: datetime = Field(description="Last activity timestamp")
    expires_at: datetime = Field(description="Session expiration time")
    is_active: bool = Field(description="Session active status")
    is_current: bool = Field(default=False, description="Is current session")


class UserInfo(BaseSchema):
    """Basic user information for authentication responses."""

    id: int = Field(description="User ID")
    username: str = Field(description="Username")
    username_alias: str = Field(description="Display name")
    email: Optional[str] = Field(None, description="Email address")
    is_active: bool = Field(description="User active status")
    is_superuser: bool = Field(description="Superuser status")
    is_verified: bool = Field(description="Account verification status")
    last_login_at: Optional[datetime] = Field(None, description="Last login timestamp")
    roles: List[str] = Field(default_factory=list, description="User role names")
    post_id: Optional[int] = Field(None, description="Assigned post ID")


class TokenPayload(BaseSchema):
    """JWT token payload."""

    sub: str = Field(description="Subject (user ID)")
    exp: datetime = Field(description="Expiration time")
    iat: datetime = Field(description="Issued at time")
    type: str = Field(description="Token type (access/refresh)")
    session_id: Optional[str] = Field(None, description="Session ID")
    permissions: List[str] = Field(default_factory=list, description="User permissions")


class CertificateValidationRequest(BaseSchema):
    """Certificate validation request."""

    certificate_data: str = Field(description="Base64 encoded certificate")
    validate_chain: bool = Field(True, description="Validate certificate chain")
    check_revocation: bool = Field(True, description="Check certificate revocation")


class CertificateValidationResponse(BaseSchema):
    """Certificate validation response."""

    is_valid: bool = Field(description="Certificate validity")
    certificate_info: Optional[PKICertificateInfo] = Field(None, description="Certificate information")
    validation_errors: List[str] = Field(default_factory=list, description="Validation errors")
    trust_chain: List[str] = Field(default_factory=list, description="Certificate trust chain")


class AuthErrorResponse(BaseSchema):
    """Authentication error response."""

    error: str = Field(description="Error type")
    error_description: str = Field(description="Error description")
    error_code: Optional[str] = Field(None, description="Error code")


class PermissionCheck(BaseSchema):
    """Permission check request."""

    permission: str = Field(description="Permission to check")
    resource_id: Optional[int] = Field(None, description="Resource ID (if applicable)")


class PermissionResponse(BaseSchema):
    """Permission check response."""

    has_permission: bool = Field(description="User has permission")
    permission: str = Field(description="Checked permission")
    reason: Optional[str] = Field(None, description="Reason if permission denied")


# Update forward references
UserInfo.model_rebuild()
LoginResponse.model_rebuild()
