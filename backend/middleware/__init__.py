"""
Middleware initialization and exports.
Provides all custom middleware classes for the FastAPI application.
"""

from app.middleware.auth import (
    AuthMiddleware,
    RateLimitMiddleware,
    SessionMiddleware,
)
from app.middleware.cors import (
    CORSMiddleware,
    CompressionMiddleware,
    SecurityHeadersMiddleware,
)
from app.middleware.logging import (
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