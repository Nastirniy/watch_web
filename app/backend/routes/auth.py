from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.ext.asyncio import AsyncSession
from pydantic import BaseModel

from app.backend.database import get_session
from app.backend.services.auth import AuthService
from app.backend.dependencies import get_current_user
from app.backend.models.user import User
from app.backend.enums import Routes

router = APIRouter()


class LoginRequest(BaseModel):
    certificate_cn: str
    certificate_serial: str
    certificate_fingerprint: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: int
    username: str


@router.post(Routes.AUTH_LOGIN.value.replace("/auth", ""), response_model=LoginResponse)
async def login(
        request: Request,
        credentials: LoginRequest,
        session: AsyncSession = Depends(get_session)
):
    """Authenticate user with PKI certificate"""
    user = await AuthService.authenticate_pki(
        session,
        credentials.certificate_cn,
        credentials.certificate_serial,
        credentials.certificate_fingerprint
    )

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid certificate credentials"
        )

    # Create session
    user_session = await AuthService.create_session(
        session,
        user,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent")
    )

    # Create token
    token = AuthService.create_access_token(user.id)

    return LoginResponse(
        access_token=token,
        user_id=user.id,
        username=user.username
    )


@router.get(Routes.AUTH_ME.value.replace("/auth", ""))
async def get_current_user_info(
        current_user: User = Depends(get_current_user)
):
    """Get current user information"""
    return {
        "id": current_user.id,
        "username": current_user.username,
        "username_alias": current_user.username_alias,
        "post_id": current_user.post_id,
        "is_active": current_user.is_active
    }