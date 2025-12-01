from typing import Optional
from pydantic import BaseModel


class LoginResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user_id: int
    username: str


class LogoutResponse(BaseModel):
    message: str


class UserInfoResponse(BaseModel):
    id: int
    username: str
    username_alias: Optional[str] = None
    post_id: Optional[int] = None
    is_active: bool
    is_admin: bool
    last_login_at: Optional[str] = None


class SessionInfoResponse(BaseModel):
    id: str
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    created_at: str
    expires_at: str
    last_activity_at: Optional[str] = None


class UserSessionsResponse(BaseModel):
    sessions: list[SessionInfoResponse]
