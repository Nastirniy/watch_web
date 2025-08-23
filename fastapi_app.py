"""
FastAPI application with lifespan management.
Main application setup with middleware, routes, and error handling.
"""

from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
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

# Add exception handlers
app.add_exception_handler(CustomHTTPException, handle_custom_http_exception)
app.add_exception_handler(ValidationException, handle_validation_exception)
app.add_exception_handler(Exception, handle_generic_exception)