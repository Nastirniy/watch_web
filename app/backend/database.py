from typing import AsyncGenerator, Optional, Dict, Any
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import text
from sqlalchemy.ext.asyncio.engine import AsyncEngine
from sqlmodel import SQLModel, select
import contextvars

from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.config import settings
from app.backend.enums.general import AppEnvironment
from app.backend.helpers.certeficates_helper import hash_data
from app.backend.utils.logging_config import get_database_logger
from app.backend.models import *  # Import all models to register them

# Get database logger
_logger = get_database_logger()

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
        echo=False,
        connect_args={}
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
    """Dependency to get database session with audit context and error handling"""
    session_maker = await get_session_maker()
    async with session_maker() as session:
        try:
            # Set audit context from context variables
            session.audit_user_id = current_user_context.get()
            session.audit_session_id = current_session_context.get()
            session.audit_request_info = current_request_context.get()
            
            # Test connection
            await session.exec(text("SELECT 1"))
            
            yield session
        except Exception as e:
            _logger.error(f"Database session error: {e}")
            await session.rollback()
            raise
        finally:
            # Ensure session is properly closed
            try:
                await session.close()
            except Exception as e:
                _logger.warning(f"Error closing session: {e}")


def _is_admin_pki_data_valid(admin_config) -> bool:
    """Check if admin PKI data is complete and appears valid"""
    # Check if all fields are present and non-empty
    if not all([
        admin_config.pki_certificate_cn,
        admin_config.pki_certificate_serial,
        admin_config.pki_certificate_fingerprint
    ]):
        return False

    # Check if values are not just empty strings
    if not all([
        admin_config.pki_certificate_cn.strip(),
        admin_config.pki_certificate_serial.strip(),
        admin_config.pki_certificate_fingerprint.strip()
    ]):
        return False

    # Check if CN looks like an email or valid format
    cn = admin_config.pki_certificate_cn.strip()
    if '@' not in cn and '=' not in cn:
        return False

    # Check if serial has reasonable length
    serial = admin_config.pki_certificate_serial.strip()
    if len(serial) < 4:
        return False

    # Check if fingerprint has reasonable format (contains colons)
    fingerprint = admin_config.pki_certificate_fingerprint.strip()
    if ':' not in fingerprint or len(fingerprint) < 20:
        return False

    return True


async def seed_database():
    """Seed database with initial data (admin user, roles, signal types)"""
    # First ensure database exists (migrations should already be applied)
    await create_database_if_not_exists()

    _logger.info("Starting database seeding...")

    # Create initial admin user and role with proper PKI certificates
    session_maker = await get_session_maker()
    async with session_maker() as session:
        from app.backend.models.user import User
        from app.backend.models.role import Role, Permission, RolePermission
        from app.backend.enums.general import Permissions
        from app.backend.utils.pki_generator import generate_admin_pki_credentials

        # Check if admin exists
        result = await session.exec(
            select(User).where(User.username == settings.admin.username)
        )
        admin_user = result.first()

        if not admin_user:
            # Determine whether to use config values or generate new PKI credentials
            use_generated = False
            admin_pki = {}

            if _is_admin_pki_data_valid(settings.admin):
                # Use configured values
                admin_pki = {
                    "pki_certificate_cn": settings.admin.pki_certificate_cn,
                    "pki_certificate_serial": settings.admin.pki_certificate_serial,
                    "pki_certificate_fingerprint": settings.admin.pki_certificate_fingerprint
                }
                _logger.info("Using admin PKI credentials from configuration")
            else:
                # Generate new PKI credentials
                try:
                    admin_pki = generate_admin_pki_credentials()
                    use_generated = True
                    _logger.info("Generated new admin PKI credentials (config data missing or invalid)")

                    # Print certificate data in dev mode
                    if settings.app.debug and settings.app.environment == AppEnvironment.DEVELOPMENT.value:
                        _logger.debug("\n" + "=" * 80)
                        _logger.debug("GENERATED ADMIN PKI CREDENTIALS (DEV MODE)")
                        _logger.debug("=" * 80)
                        _logger.debug(f"CN:          {admin_pki['pki_certificate_cn']}")
                        _logger.debug(f"Serial:      {admin_pki['pki_certificate_serial']}")
                        _logger.debug(f"Fingerprint: {admin_pki['pki_certificate_fingerprint']}")
                        _logger.debug("=" * 80 + "\n")
                except Exception as e:
                    _logger.error(f"Failed to generate PKI certificates and no valid config data available: {e}")
                    raise ValueError("Cannot create admin user without valid PKI credentials")

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

            # Create admin user with PKI credentials (hashed)
            from app.backend.services.auth import AuthService
            admin_user = User(
                username=settings.admin.username,
                username_alias=settings.admin.username_alias,
                pki_certificate_cn_hash=await hash_data(admin_pki["pki_certificate_cn"]),
                pki_certificate_serial_hash=await hash_data(admin_pki["pki_certificate_serial"]),
                pki_certificate_fingerprint_hash=await hash_data(admin_pki["pki_certificate_fingerprint"]),
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

            method = "generated" if use_generated else "configured"
            _logger.info(f"Admin user created with {method} PKI credentials (CN: {admin_pki['pki_certificate_cn']})")
        else:
            _logger.info("Admin user already exists")

        # Seed signal types if they don't exist
        from app.backend.models.observation import SignalType
        result = await session.exec(select(SignalType))
        existing_signal_types = result.all()
        
        if not existing_signal_types:
            signal_types_data = [
                {"id": 1, "code": "fpv_drone", "name": "FPV дрон"},
                {"id": 2, "code": "wing_uav", "name": "Крило (БПЛА)"},
                {"id": 3, "code": "mavic", "name": "Mavic"},
            ]
            
            for signal_data in signal_types_data:
                signal_type = SignalType(
                    code=signal_data["code"],
                    name=signal_data["name"],
                    is_active=True,
                    sort_order=signal_data["id"]
                )
                session.add(signal_type)
            
            await session.commit()
            _logger.info("Signal types seeded successfully")
        else:
            _logger.info("Signal types already exist")


async def initialize_database_with_migrations():
    """Initialize database using migrations and then seed data"""
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from alembic.config import Config
    from alembic import command
    
    _logger.info("Initializing database with migrations...")
    
    try:
        # Ensure database exists
        await create_database_if_not_exists()
        
        # Check if we need to run migrations
        def check_and_run_migrations():
            try:
                from pathlib import Path
                # Get the project root directory (where alembic.ini is located)
                project_root = Path(__file__).parent.parent.parent
                alembic_ini_path = project_root / "alembic.ini"
                alembic_cfg = Config(str(alembic_ini_path))
                
                # Try to upgrade to head (this handles both fresh DB and updates)
                command.upgrade(alembic_cfg, "head")
                return True
                
            except Exception as e:
                _logger.warning(f"Migration failed, will create tables manually: {e}")
                return False
        
        # Run migrations in thread to avoid event loop issues
        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor() as executor:
            migration_success = await loop.run_in_executor(executor, check_and_run_migrations)
        
        if migration_success:
            _logger.info("Migrations applied successfully")
        else:
            # Fallback: create tables manually if migration fails
            _logger.info("Creating tables manually as fallback...")
            engine = await get_engine()
            async with engine.begin() as conn:
                await conn.run_sync(SQLModel.metadata.create_all)
            _logger.info("Tables created manually")
        
        # Always seed initial data
        await seed_database()
        _logger.info("Database initialization completed successfully")
        
    except Exception as e:
        _logger.error(f"Failed to initialize database: {e}")
        raise


async def close_db():
    """Close database connections"""
    global engine
    if engine:
        await engine.dispose()
        engine = None
        _logger.info("Database connections closed")
