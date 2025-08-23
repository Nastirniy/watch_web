"""
FastAPI application with lifespan management.
Main application setup with middleware, routes, and error handling.
"""

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger

from backend.core.config.config import settings
from backend.core.exceptions import (
    CustomHTTPException,
    ValidationException,
    handle_custom_http_exception,
    handle_validation_exception,
    handle_generic_exception,
)
from backend.database.db_controls import close_db_connection, create_db_and_tables
from backend.middleware.auth import AuthMiddleware
from backend.middleware.logging import LoggingMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Application lifespan manager.
    Handles startup and shutdown events.
    """
    # Startup
    logger.info("Starting up Signal Monitoring API...")

    try:
        # Create database tables
        await create_db_and_tables()

        # Initialize other services here if needed
        # e.g., Redis connection, background tasks, etc.

        logger.info("Application startup completed successfully")

    except Exception as e:
        logger.error(f"Failed to start application: {e}")
        raise

    yield

    # Shutdown
    logger.info("Shutting down Signal Monitoring API...")

    try:
        # Close database connections
        await close_db_connection()

        # Clean up other resources here if needed

        logger.info("Application shutdown completed successfully")

    except Exception as e:
        logger.error(f"Error during shutdown: {e}")


# Create FastAPI application
app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="API for signal monitoring and management system",
    debug=settings.DEBUG,
    lifespan=lifespan,
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url="/redoc" if settings.DEBUG else None,
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.BACKEND_CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "PATCH"],
    allow_headers=["*"],
)

# Add custom middleware
app.add_middleware(LoggingMiddleware)
app.add_middleware(AuthMiddleware)

# Add exception handlers
app.add_exception_handler(CustomHTTPException, handle_custom_http_exception)
app.add_exception_handler(ValidationException, handle_validation_exception)
app.add_exception_handler(Exception, handle_generic_exception)


# Health check endpoint
@app.get("/health", tags=["Health"])
async def health_check() -> dict:
    """Health check endpoint."""
    return {
        "status": "healthy",
        "app_name": settings.APP_NAME,
        "version": settings.APP_VERSION,
    }


# Root endpoint
@app.get("/", tags=["Root"])
async def root() -> dict:
    """Root endpoint with API information."""
    return {
        "message": f"Welcome to {settings.APP_NAME}",
        "version": settings.APP_VERSION,
        "docs_url": "/docs" if settings.DEBUG else None,
    }


# Include API routers
from app.api import auth, exports, media, posts, signals, users  # noqa: E402

app.include_router(auth.router, prefix=f"{settings.API_V1_STR}/auth", tags=["Authentication"])
app.include_router(users.router, prefix=f"{settings.API_V1_STR}/users", tags=["Users"])
app.include_router(posts.router, prefix=f"{settings.API_V1_STR}/posts", tags=["Posts"])
app.include_router(signals.router, prefix=f"{settings.API_V1_STR}/signals", tags=["Signals"])
app.include_router(media.router, prefix=f"{settings.API_V1_STR}/media", tags=["Media"])
app.include_router(exports.router, prefix=f"{settings.API_V1_STR}/exports", tags=["Exports"])
