"""Database engine and sessions: get_session is the per-request dependency, get_session_maker serves scripts
and workers. What runs at startup (migrations, seeding) is app/backend/bootstrap."""
from typing import AsyncGenerator, Optional
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import text
from sqlalchemy.ext.asyncio.engine import AsyncEngine

from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.config import settings
from app.backend.utils.logging_config import get_database_logger
from app.backend.models import *  # Import all models to register them

# Get database logger
_logger = get_database_logger()

async def create_database_if_not_exists():
    """Create database if it doesn't exist"""
    # Connect without database to create it if needed
    db_config = settings.database
    engine_without_db = create_async_engine(
        f"mysql+aiomysql://{db_config.user}:{db_config.password}@{db_config.host}:{db_config.port}/",
        echo=False,
        connect_args={}
    )

    async with engine_without_db.begin() as conn:
        # Check if database exists
        result = await conn.execute(
            text("SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME = :db_name"),
            {"db_name": db_config.name}
        )
        exists = result.fetchone() is not None

        if not exists:
            # Create database with proper charset and collation
            await conn.execute(
                text(f"CREATE DATABASE {db_config.name} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
            )
            _logger.info(f"Database '{db_config.name}' created successfully")
        else:
            _logger.info(f"Database '{db_config.name}' already exists")

    await engine_without_db.dispose()


# Create engine (will be initialized after database check)
engine: Optional[AsyncEngine] = None
async_session_maker = None


async def get_engine() -> AsyncEngine:
    """Get or create database engine"""
    global engine
    if engine is None:
        engine = create_async_engine(
            settings.database.url,
            echo=settings.database.echo,
            pool_size=settings.database.pool_size,
            max_overflow=settings.database.max_overflow,
            pool_timeout=30,  # Wait up to 30 seconds for a connection
            pool_recycle=3600,  # Recycle connections after 1 hour
            pool_pre_ping=True,  # Verify connections before use
            connect_args={
                "connect_timeout": 10,
                "charset": "utf8mb4",
                "use_unicode": True,
                "autocommit": False,
            },
            future=True
        )
        _logger.info(f"Database engine created with pool_size={settings.database.pool_size}, max_overflow={settings.database.max_overflow}")
    return engine


async def create_events_engine() -> AsyncEngine:
    """The engine of the event hub of this process (services/events/hub.py): pool 1, overflow 1, its own, so the pump
    never waits for the request pool and API load never delays it (2 connections per api worker in the budget)"""
    return create_async_engine(
        settings.database.url,
        echo=settings.database.echo,
        pool_size=1,
        max_overflow=1,
        pool_timeout=5,
        pool_recycle=3600,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 10, "charset": "utf8mb4", "use_unicode": True, "autocommit": False},
    )


async def get_session_maker():
    """Get or create session maker"""
    global async_session_maker
    if async_session_maker is None:
        engine = await get_engine()
        async_session_maker = async_sessionmaker(
            engine,
            class_=AsyncSession,
            expire_on_commit=False
        )
    return async_session_maker


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Dependency: one database session per request (connections are checked by pool_pre_ping).

    Any error of the request rolls the session back and goes on unlogged: an ApiError is the answer, and an
    unhandled error is logged once, with its error_id, by UnhandledErrorMiddleware.
    """
    session_maker = await get_session_maker()
    async with session_maker() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def close_db():
    """Close database connections"""
    global engine
    if engine:
        await engine.dispose()
        engine = None
        _logger.info("Database connections closed")
