from typing import Optional, List
from datetime import datetime, timezone
import time
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response, JSONResponse
from fastapi import status
from jose import jwt, JWTError
from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession
from sqlmodel import select

from app.backend.config import settings
from app.backend.utils.logging_config import get_auth_logger
from app.backend.database import get_session_maker
from app.backend.models.user import User, UserSession
from app.backend.enums.routes import AuthRoutes, SystemRoutes, DocsRoutes

logger = get_auth_logger()


class SecurityMiddleware(BaseHTTPMiddleware):
    """Enhanced security middleware with JWT authentication, rate limiting, and security headers"""

    # Public endpoints that don't require authentication
    PUBLIC_PATHS = [
        SystemRoutes.ROOT.value,
        DocsRoutes.SWAGGER_UI.value,
        DocsRoutes.REDOC.value,
        SystemRoutes.OPENAPI_JSON.value,
        AuthRoutes.LOGIN.versioned_path(),
        SystemRoutes.HEALTH.value,
        SystemRoutes.FAVICON.value
    ]

    # Rate limiting storage (in production, use Redis)
    rate_limit_storage = {}

    async def dispatch(self, request: Request, call_next) -> Response:
        start_time = time.time()
        
        try:
            # Apply rate limiting
            if not await self._check_rate_limit(request):
                logger.warning(f"Rate limit exceeded for {self._get_client_ip(request)}")
                return JSONResponse(
                    status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                    content={"error": "Rate limit exceeded", "retry_after": 60}
                )

            # Skip auth for public paths
            if self._is_public_path(request.url.path):
                response = await call_next(request)
            else:
                # Authenticate request
                auth_result = await self._authenticate_request(request)
                if not auth_result["success"]:
                    logger.warning(f"Authentication failed: {auth_result['error']} for {self._get_client_ip(request)}")
                    return JSONResponse(
                        status_code=status.HTTP_401_UNAUTHORIZED,
                        content={"error": auth_result["error"]},
                        headers={"WWW-Authenticate": "Bearer"}
                    )

                # Add user context to request
                request.state.user_id = auth_result["user_id"]
                request.state.session_id = auth_result["session_id"]
                
                response = await call_next(request)

            # Add security headers
            self._add_security_headers(response)
            
            # Log request
            process_time = time.time() - start_time
            logger.info(
                f"{request.method} {request.url.path} - "
                f"{response.status_code} - {process_time:.3f}s - "
                f"IP: {self._get_client_ip(request)}"
            )
            
            return response

        except Exception as e:
            logger.error(f"Security middleware error: {str(e)}")
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={"error": "Internal server error"}
            )

    def _is_public_path(self, path: str) -> bool:
        """Check if path is public and doesn't require authentication"""
        return any(path.startswith(public_path) for public_path in self.PUBLIC_PATHS)

    async def _authenticate_request(self, request: Request) -> dict:
        """Authenticate JWT token and validate session"""
        try:
            # Get token from Authorization header
            auth_header = request.headers.get("Authorization")
            if not auth_header:
                return {"success": False, "error": "Missing Authorization header"}

            if not auth_header.startswith("Bearer "):
                return {"success": False, "error": "Invalid Authorization header format"}

            token = auth_header[7:]  # Remove "Bearer " prefix

            # Decode JWT token
            try:
                payload = jwt.decode(
                    token,
                    settings.security.secret_key,
                    algorithms=[settings.security.algorithm],
                    leeway=settings.security.jwt_leeway_seconds
                )
            except JWTError as e:
                return {"success": False, "error": f"Invalid token: {str(e)}"}

            user_id = int(payload.get("sub"))
            token_type = payload.get("type")

            if token_type != "access":
                return {"success": False, "error": "Invalid token type"}

            # Validate user and session in database
            from app.backend.database import engine
            async with AsyncSession(engine) as db_session:
                # Check if user exists and is active
                user_result = await db_session.exec(
                    select(User).where(
                        User.id == user_id,
                        User.is_active == True,
                        User.is_deleted == False
                    )
                )
                user = user_result.first()

                if not user:
                    return {"success": False, "error": "User not found or inactive"}

                # Check for valid active session
                session_result = await db_session.exec(
                    select(UserSession).where(
                        UserSession.user_id == user_id,
                        UserSession.expires_at > datetime.now(timezone.utc),
                        UserSession.is_active == True
                    )
                )
                user_session = session_result.first()

                if not user_session:
                    return {"success": False, "error": "No valid session found"}

                return {
                    "success": True,
                    "user_id": user_id,
                    "session_id": user_session.id
                }

        except Exception as e:
            logger.error(f"Authentication error: {str(e)}")
            return {"success": False, "error": "Authentication failed"}

    async def _check_rate_limit(self, request: Request) -> bool:
        """Simple rate limiting implementation (60 requests per minute per IP)"""
        client_ip = self._get_client_ip(request)
        current_time = time.time()
        
        # Clean old entries
        self.rate_limit_storage = {
            ip: timestamps for ip, timestamps in self.rate_limit_storage.items()
            if any(t > current_time - 60 for t in timestamps)
        }
        
        # Check current IP
        if client_ip not in self.rate_limit_storage:
            self.rate_limit_storage[client_ip] = []
        
        # Remove timestamps older than 1 minute
        self.rate_limit_storage[client_ip] = [
            t for t in self.rate_limit_storage[client_ip] 
            if t > current_time - 60
        ]
        
        # Check limit (60 requests per minute)
        if len(self.rate_limit_storage[client_ip]) >= 60:
            return False
        
        # Add current request
        self.rate_limit_storage[client_ip].append(current_time)
        return True

    def _get_client_ip(self, request: Request) -> str:
        """Get client IP address with proxy support"""
        # Check for forwarded headers (when behind proxy)
        forwarded_for = request.headers.get("X-Forwarded-For")
        if forwarded_for:
            return forwarded_for.split(",")[0].strip()
        
        forwarded = request.headers.get("X-Forwarded")
        if forwarded:
            return forwarded.split(",")[0].strip()
        
        real_ip = request.headers.get("X-Real-IP")
        if real_ip:
            return real_ip
        
        # Fallback to client host
        return request.client.host if request.client else "unknown"

    def _add_security_headers(self, response: Response) -> None:
        """Add comprehensive security headers"""
        # Security headers for all responses
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' 'unsafe-inline'; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: https:; "
            "connect-src 'self'; "
            "font-src 'self'; "
            "object-src 'none'; "
            "media-src 'self'; "
            "frame-ancestors 'none'"
        )
        
        # HTTPS enforcement headers (only in production)
        if settings.security.require_https and settings.app.environment == "production":
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains; preload"
            )

# Keep backwards compatibility
AuthMiddleware = SecurityMiddleware