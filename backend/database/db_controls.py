"""
Database connection and session management.
Handles async database connections with SQLModel and MariaDB.
"""

from typing import AsyncGenerator

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import QueuePool
from sqlmodel import SQLModel

from backend.core.config.config import settings

# Create async engine with connection pooling
engine = create_async_engine(
    settings.async_database_url,
    echo=settings.DATABASE_ECHO,
    poolclass=QueuePool,
    pool_size=settings.DATABASE_POOL_SIZE,
    max_overflow=settings.DATABASE_MAX_OVERFLOW,
    pool_timeout=settings.DATABASE_POOL_TIMEOUT,
    pool_pre_ping=True,  # Verify connections before use
    pool_recycle=3600,  # Recycle connections after 1 hour
)

# Create async session factory
AsyncSessionLocal = async_sessionmaker(
    engine,
    class_=AsyncSession,
    expire_on_commit=False,
    autoflush=False,
    autocommit=False,
)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Dependency function to get database session.
    Ensures proper session cleanup after request.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception as e:
            logger.error(f"Database session error: {e}")
            await session.rollback()
            raise
        finally:
            await session.close()


async def create_db_and_tables() -> None:
    """
    Create database tables if they don't exist.
    Called during application startup.
    """
    try:
        async with engine.begin() as conn:
            # Import all models to register them with SQLModel
            from app.models import (  # noqa: F401
                audit,
                export,
                media,
                post,
                signal,
                user,
            )

            # Create all tables
            await conn.run_sync(SQLModel.metadata.create_all)
            logger.info("Database tables created successfully")

    except Exception as e:
        logger.error(f"Failed to create database tables: {e}")
        raise


async def close_db_connection() -> None:
    """
    Close database connections.
    Called during application shutdown.
    """
    try:
        await engine.dispose()
        logger.info("Database connections closed successfully")
    except Exception as e:
        logger.error(f"Failed to close database connections: {e}")
        raise


# Database session dependency
DatabaseSession = AsyncSession
