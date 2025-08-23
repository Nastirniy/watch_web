"""
Middleware initialization and exports.
Provides all custom middleware classes for the FastAPI application.
"""
import fastapi_app
from backend.core.config.config import settings
from backend.middleware.auth import (
    AuthMiddleware,
    RateLimitMiddleware,
    SessionMiddleware,
)
from backend.middleware.cors import (
    CORSMiddleware,
    CompressionMiddleware,
    SecurityHeadersMiddleware,
)
from backend.middleware.logging import (
    LoggingMiddleware,
    RequestContextMiddleware,
    SecurityLoggingMiddleware,
    setup_logging,
)

# Export all middleware classes
__all__ = [
    # Authentication middleware
    "AuthMiddleware",
    "RateLimitMiddleware",
    "SessionMiddleware",

    # CORS and security middleware
    "CORSMiddleware",
    "SecurityHeadersMiddleware",
    "CompressionMiddleware",

    # Logging middleware
    "LoggingMiddleware",
    "RequestContextMiddleware",
    "SecurityLoggingMiddleware",
    "setup_logging",
]


# Add CORS middleware
fastapi_app.app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.BACKEND_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    allow_headers=["*"],
)

# Add custom middleware
fastapi_app.app.add_middleware(LoggingMiddleware)
fastapi_app.app.add_middleware(AuthMiddleware)
