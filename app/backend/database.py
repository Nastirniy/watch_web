from typing import AsyncGenerator, Optional, Dict, Any
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.orm import sessionmaker
from sqlalchemy import event, text
from sqlmodel import SQLModel, select
from loguru import logger
import contextvars

from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.config import settings
from app.backend.models import *  # Import all models to register them

# Context variable to store current user ID for audit logging
current_user_context = contextvars.ContextVar('current_user_id', default=None)
current_session_context = contextvars.ContextVar('current_session_id', default=None)
current_request_context = contextvars.ContextVar('current_request_info', default={})


async def create_database_if_not_exists():
    """Create database if it doesn't exist"""
    # Connect without database to create it if needed
    db_config = settings.database
    engine_without_db = create_async_engine(
        f"mysql+aiomysql://{db_config.user}:{db_config.password}@{db_config.host}:{db_config.port}/",
        echo=False
    )

    async with engine_without_db.begin() as conn:
        # Check if database exists
        result = await conn.execute(
            text(f"SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA WHERE SCHEMA_NAME = :db_name"),
            {"db_name": db_config.name}
        )
        exists = result.fetchone() is not None

        if not exists:
            # Create database with proper charset and collation
            await conn.execute(
                text(f"CREATE DATABASE {db_config.name} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
            )
            logger.info(f"Database '{db_config.name}' created successfully")
        else:
            logger.info(f"Database '{db_config.name}' already exists")

    await engine_without_db.dispose()


# Create engine (will be initialized after database check)
engine = None
async_session_maker = None


async def get_engine():
    """Get or create database engine"""
    global engine
    if engine is None:
        engine = create_async_engine(
            settings.database.url,
            echo=settings.database.echo,
            pool_size=settings.database.pool_size,
            max_overflow=settings.database.max_overflow,
            future=True
        )
    return engine


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


class AuditedSession(AsyncSession):
    """Extended session with audit context"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.audit_user_id: Optional[int] = None
        self.audit_session_id: Optional[str] = None
        self.audit_request_info: Dict[str, Any] = {}


async def get_session() -> AsyncGenerator[AuditedSession, None]:
    """Dependency to get database session with audit context"""
    session_maker = await get_session_maker()
    async with session_maker() as session:
        # Set audit context from context variables
        session.audit_user_id = current_user_context.get()
        session.audit_session_id = current_session_context.get()
        session.audit_request_info = current_request_context.get()
        yield session


async def init_db():
    """Initialize database and create admin user"""
    # First ensure database exists
    await create_database_if_not_exists()

    # Get engine and create tables
    engine = await get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)

    logger.info("Database tables created")

    # Create initial admin user and role
    session_maker = await get_session_maker()
    async with session_maker() as session:
        from app.backend.models.user import User
        from app.backend.models.role import Role, Permission, RolePermission
        from app.backend.enums import Permissions

        # Check if admin exists
        result = await session.exec(
            select(User).where(User.username == settings.admin.username)
        )
        admin_user = result.first()

        if not admin_user:
            # Create admin role with all permissions
            admin_role = Role(
                name="admin",
                display_name="Administrator",
                description="Full system access"
            )
            session.add(admin_role)
            await session.flush()

            # Create all permissions
            for perm in Permissions:
                permission = Permission(name=perm.value)
                session.add(permission)
                await session.flush()

                # Assign to admin role
                role_perm = RolePermission(
                    role_id=admin_role.id,
                    permission_id=permission.id
                )
                session.add(role_perm)

            # Create admin user
            admin_user = User(
                username=settings.admin.username,
                username_alias=settings.admin.username_alias,
                pki_certificate_cn=settings.admin.pki_certificate_cn,
                pki_certificate_serial=settings.admin.pki_certificate_serial,
                pki_certificate_fingerprint=settings.admin.pki_certificate_fingerprint,
                is_active=True
            )
            session.add(admin_user)
            await session.flush()

            # Assign admin role to user
            from app.backend.models.role import UserRole
            user_role = UserRole(
                user_id=admin_user.id,
                role_id=admin_role.id
            )
            session.add(user_role)

            await session.commit()
            logger.info("Admin user and permissions created")
        else:
            logger.info("Admin user already exists")


async def close_db():
    """Close database connections"""
    global engine
    if engine:
        await engine.dispose()
        engine = None
        logger.info("Database connections closed")

