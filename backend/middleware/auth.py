"""
Authentication middleware for JWT token validation and user context.
Handles session management, token refresh, and security logging.
"""

import re
from typing import Callable, Optional, Set

from fastapi import Request, Response
from jose import JWTError
from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from backend.core.security import decode_access_token
from backend.database import AsyncSessionLocal
from backend.database.crud.user import user as user_crud, user_session as session_crud
from backend.database.crud.audit import audit_log as audit_crud
from backend.models.audit import AuditAction, ResourceType, AuditSeverity


class AuthMiddleware(BaseHTTPMiddleware):
    """Middleware for handling authentication and user context."""

    def __init__(self, app: ASGIApp):
        """
        Initialize authentication middleware.

        Args:
            app: ASGI application
        """
        super().__init__(app)

        # Public endpoints that don't require authentication
        self.public_endpoints: Set[str] = {
            "/",
            "/health",
            "/docs",
            "/redoc",
            "/openapi.json",
            "/api/v1/auth/login",
            "/api/v1/auth/certificate/validate",
        }

        # Endpoints that require authentication but are less strict
        self.optional_auth_patterns = [
            re.compile(r"^/api/v1/posts/\d+$"),  # GET posts by ID
            re.compile(r"^/api/v1/signals/types$"),  # GET signal types
        ]

        # Admin-only endpoints
        self.admin_endpoints: Set[str] = {
            "/api/v1/users",
            "/api/v1/roles",
            "/api/v1/permissions",
            "/api/v1/system",
        }

        # Rate limiting counters (in production, use Redis)
        self.rate_limit_counters = {}

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """
        Process authentication for each request.

        Args:
            request: HTTP request
            call_next: Next middleware/handler

        Returns:
            HTTP response with authentication context
        """
        path = request.url.path
        method = request.method.upper()

        # Initialize user context
        request.state.user = None
        request.state.session_id = None
        request.state.is_authenticated = False
        request.state.permissions = []

        # Skip authentication for public endpoints
        if self._is_public_endpoint(path, method):
            return await call_next(request)

        # Extract token from request
        token = self._extract_token(request)

        # Check if authentication is optional for this endpoint
        is_optional_auth = self._is_optional_auth_endpoint(path, method)

        if not token:
            if is_optional_auth:
                # Continue without authentication
                return await call_next(request)
            else:
                # Authentication required but no token provided
                await self._log_auth_failure(request, "No authentication token provided")
                return self._create_unauthorized_response("Authentication required")

        # Validate token and get user
        try:
            user, session_id = await self._authenticate_token(token)

            if not user:
                await self._log_auth_failure(request, "Invalid or expired token")
                return self._create_unauthorized_response("Invalid authentication token")

            if not user.is_active:
                await self._log_auth_failure(request, f"Inactive user: {user.username}")
                return self._create_unauthorized_response("Account is inactive")

            # Set user context
            request.state.user = user
            request.state.session_id = session_id
            request.state.is_authenticated = True
            request.state.permissions = user.get_permissions()

            # Check admin access if required
            if self._requires_admin_access(path) and not user.is_superuser:
                await self._log_auth_failure(
                    request,
                    f"Insufficient privileges for user: {user.username}"
                )
                return self._create_forbidden_response("Insufficient privileges")

            # Update session activity
            async with AsyncSessionLocal() as db:
                await session_crud.refresh_session(db, session_id)

            # Log successful authentication
            await self._log_auth_success(request, user)

        except JWTError as e:
            await self._log_auth_failure(request, f"JWT validation failed: {str(e)}")

            if is_optional_auth:
                # Continue without authentication for optional endpoints
                return await call_next(request)
            else:
                return self._create_unauthorized_response("Invalid authentication token")

        except Exception as e:
            logger.error(f"Authentication error: {str(e)}")
            await self._log_auth_failure(request, f"Authentication system error: {str(e)}")

            if is_optional_auth:
                return await call_next(request)
            else:
                return self._create_unauthorized_response("Authentication system error")

        return await call_next(request)

    def _extract_token(self, request: Request) -> Optional[str]:
        """
        Extract authentication token from request.

        Args:
            request: HTTP request

        Returns:
            Bearer token or None
        """
        # Check Authorization header
        auth_header = request.headers.get("authorization")
        if auth_header and auth_header.startswith("Bearer "):
            return auth_header[7:]  # Remove "Bearer " prefix

        # Check query parameter (for downloads, etc.)
        token = request.query_params.get("token")
        if token:
            return token

        # Check cookie (if using cookie-based auth)
        token = request.cookies.get("access_token")
        if token:
            return token

        return None

    async def _authenticate_token(self, token: str) -> tuple[Optional[object], Optional[str]]:
        """
        Authenticate user from JWT token.

        Args:
            token: JWT access token

        Returns:
            Tuple of (user, session_id) or (None, None)
        """
        try:
            # Decode and validate token
            payload = decode_access_token(token)

            # Extract user ID and session ID
            user_id = payload.get("sub")
            session_id = payload.get("session_id")

            if not user_id:
                return None, None

            # Get user from database
            async with AsyncSessionLocal() as db:
                user = await user_crud.get_with_roles(db, int(user_id))

                # Validate session if session_id is present
                if session_id:
                    session = await session_crud.get_active_session(db, session_id)
                    if not session or session.user_id != int(user_id):
                        logger.warning(f"Invalid session {session_id} for user {user_id}")
                        return None, None

                return user, session_id

        except (ValueError, TypeError) as e:
            logger.warning(f"Token authentication failed: {str(e)}")
            return None, None

    def _is_public_endpoint(self, path: str, method: str) -> bool:
        """
        Check if endpoint is public and doesn't require authentication.

        Args:
            path: Request path
            method: HTTP method

        Returns:
            True if public endpoint
        """
        # Exact path matches
        if path in self.public_endpoints:
            return True

        # Static files and documentation
        if path.startswith(("/static/", "/favicon.ico")):
            return True

        # Health check endpoints
        if path.startswith("/health"):
            return True

        # OpenAPI documentation
        if path in ["/docs", "/redoc", "/openapi.json"]:
            return True

        return False

    def _is_optional_auth_endpoint(self, path: str, method: str) -> bool:
        """
        Check if endpoint has optional authentication.

        Args:
            path: Request path
            method: HTTP method

        Returns:
            True if authentication is optional
        """
        # Only for GET requests
        if method != "GET":
            return False

        # Check against optional auth patterns
        return any(pattern.match(path) for pattern in self.optional_auth_patterns)

    def _requires_admin_access(self, path: str) -> bool:
        """
        Check if endpoint requires admin access.

        Args:
            path: Request path

        Returns:
            True if admin access required
        """
        return any(path.startswith(admin_path) for admin_path in self.admin_endpoints)

    def _create_unauthorized_response(self, message: str) -> Response:
        """
        Create unauthorized response.

        Args:
            message: Error message

        Returns:
            401 Unauthorized response
        """
        return Response(
            content=f'{{"success": false, "message": "{message}"}}',
            status_code=401,
            headers={
                "WWW-Authenticate": "Bearer",
                "Content-Type": "application/json"
            }
        )

    def _create_forbidden_response(self, message: str) -> Response:
        """
        Create forbidden response.

        Args:
            message: Error message

        Returns:
            403 Forbidden response
        """
        return Response(
            content=f'{{"success": false, "message": "{message}"}}',
            status_code=403,
            headers={"Content-Type": "application/json"}
        )

    async def _log_auth_success(self, request: Request, user) -> None:
        """
        Log successful authentication.

        Args:
            request: HTTP request
            user: Authenticated user
        """
        try:
            async with AsyncSessionLocal() as db:
                await audit_crud.create_log(
                    db=db,
                    action=AuditAction.LOGIN,
                    resource_type=ResourceType.USER,
                    user_id=user.id,
                    session_id=getattr(request.state, "session_id", None),
                    resource_name=user.username,
                    ip_address=request.client.host if request.client else None,
                    user_agent=request.headers.get("user-agent"),
                    request_url=str(request.url),
                    request_method=request.method,
                    success=True,
                    severity=AuditSeverity.LOW
                )
        except Exception as e:
            logger.error(f"Failed to log auth success: {str(e)}")

    async def _log_auth_failure(self, request: Request, reason: str) -> None:
        """
        Log authentication failure.

        Args:
            request: HTTP request
            reason: Failure reason
        """
        try:
            async with AsyncSessionLocal() as db:
                await audit_crud.create_log(
                    db=db,
                    action=AuditAction.LOGIN_FAILED,
                    resource_type=ResourceType.USER,
                    ip_address=request.client.host if request.client else None,
                    user_agent=request.headers.get("user-agent"),
                    request_url=str(request.url),
                    request_method=request.method,
                    success=False,
                    error_message=reason,
                    severity=AuditSeverity.MEDIUM
                )
        except Exception as e:
            logger.error(f"Failed to log auth failure: {str(e)}")


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Simple rate limiting middleware (use Redis in production)."""

    def __init__(self, app: ASGIApp, requests_per_minute: int = 60):
        """
        Initialize rate limiting middleware.

        Args:
            app: ASGI application
            requests_per_minute: Rate limit threshold
        """
        super().__init__(app)
        self.requests_per_minute = requests_per_minute
        self.rate_counters = {}  # In production, use Redis

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """
        Apply rate limiting to requests.

        Args:
            request: HTTP request
            call_next: Next middleware/handler

        Returns:
            HTTP response or rate limit error
        """
        # Skip rate limiting for health checks and static files
        path = request.url.path
        if path in ["/health", "/docs", "/redoc", "/openapi.json"]:
            return await call_next(request)

        # Get client identifier
        client_ip = request.client.host if request.client else "unknown"
        user_id = getattr(request.state, "user", {})
        if hasattr(user_id, "id"):
            client_key = f"user:{user_id.id}"
        else:
            client_key = f"ip:{client_ip}"

        # Check rate limit (simplified implementation)
        import time
        now = int(time.time() / 60)  # Current minute

        if client_key not in self.rate_counters:
            self.rate_counters[client_key] = {}

        # Clean old entries
        old_keys = [k for k in self.rate_counters[client_key].keys() if k < now - 1]
        for old_key in old_keys:
            del self.rate_counters[client_key][old_key]

        # Count requests in current minute
        current_count = self.rate_counters[client_key].get(now, 0)

        if current_count >= self.requests_per_minute:
            logger.warning(f"Rate limit exceeded for {client_key}")
            return Response(
                content='{"success": false, "message": "Rate limit exceeded"}',
                status_code=429,
                headers={
                    "Content-Type": "application/json",
                    "Retry-After": "60"
                }
            )

        # Increment counter
        self.rate_counters[client_key][now] = current_count + 1

        return await call_next(request)


class SessionMiddleware(BaseHTTPMiddleware):
    """Middleware for session management and cleanup."""

    def __init__(self, app: ASGIApp):
        """
        Initialize session middleware.

        Args:
            app: ASGI application
        """
        super().__init__(app)
        self._last_cleanup = 0

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """
        Handle session management.

        Args:
            request: HTTP request
            call_next: Next middleware/handler

        Returns:
            HTTP response
        """
        # Periodic cleanup of expired sessions (every 10 minutes)
        import time
        now = time.time()
        if now - self._last_cleanup > 600:  # 10 minutes
            try:
                async with AsyncSessionLocal() as db:
                    cleaned = await session_crud.cleanup_expired_sessions(db)
                    if cleaned > 0:
                        logger.info(f"Cleaned up {cleaned} expired sessions")
                self._last_cleanup = now
            except Exception as e:
                logger.error(f"Session cleanup failed: {str(e)}")

        response = await call_next(request)

        # Update session activity if user is authenticated
        if hasattr(request.state, "session_id") and request.state.session_id:
            try:
                async with AsyncSessionLocal() as db:
                    await session_crud.refresh_session(db, request.state.session_id)
            except Exception as e:
                logger.debug(f"Failed to update session activity: {str(e)}")

        return response
