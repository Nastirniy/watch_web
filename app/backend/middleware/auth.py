from typing import Optional
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response, JSONResponse
from loguru import logger

from app.backend.services.permissions import PermissionService


class AuthMiddleware(BaseHTTPMiddleware):
    """Authentication and permission middleware"""

    # Public endpoints that don't require authentication
    PUBLIC_PATHS = [
        "/docs",
        "/redoc",
        "/openapi.json",
        "/api/auth/login",
        "/health"
    ]

    async def dispatch(self, request: Request, call_next) -> Response:
        # Skip auth for public paths
        if any(request.url.path.startswith(path) for path in self.PUBLIC_PATHS):
            return await call_next(request)

        # Log request
        logger.debug(f"{request.method} {request.url.path}")

        # Process request
        response = await call_next(request)

        return response