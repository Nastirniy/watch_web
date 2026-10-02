from typing import AsyncGenerator, Optional
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy import text
from sqlalchemy.ext.asyncio.engine import AsyncEngine
from sqlmodel import SQLModel, select

from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.config import settings
from app.backend.enums.general import AppEnvironment
from app.backend.helpers.certeficates_helper import hash_data
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


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """Dependency: one database session per request (connections are checked by pool_pre_ping)"""
    session_maker = await get_session_maker()
    async with session_maker() as session:
        try:
            yield session
        except Exception as e:
            _logger.error(f"Database session error: {e}")
            await session.rollback()
            raise


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


ADMIN_ROLE_NAME = "admin"  # built-in role: seeded, cannot be archived or deleted

PERMISSION_META = {
    "user.view": ("Перегляд користувачів", "Дозволяє переглядати інформацію та профілі користувачів"),
    "user.create": ("Створення користувачів", "Дозволяє створювати нові облікові записи користувачів"),
    "user.update": ("Оновлення користувачів", "Дозволяє змінювати інформацію та налаштування користувачів"),
    "user.archive": ("Архівування користувачів", "Дозволяє архівувати та відновлювати облікові записи користувачів. Архівований користувач втрачає доступ і зникає зі списків, але його дані та спостереження зберігаються; також дозволяє бачити архівованих у списках (include_deleted)"),
    "post.view": ("Перегляд посту", "Дозволяє переглядати пости та їх деталі"),
    "post.create": ("Створення посту", "Дозволяє створювати нові пости"),
    "post.update": ("Оновлення посту", "Дозволяє редагувати існуючі пости"),
    "post.archive": ("Архівування постів", "Дозволяє архівувати та відновлювати пости. Дані не видаляються"),
    "observation.view": ("Перегляд спостережень", "Дозволяє переглядати сигнальні спостереження та їх дані"),
    "observation.create": ("Створення спостережень", "Дозволяє створювати нові сигнальні спостереження"),
    "observation.update": ("Оновлення спостережень", "Дозволяє змінювати існуючі сигнальні спостереження"),
    "observation.archive": ("Архівування спостережень", "Дозволяє архівувати та відновлювати власні спостереження (чужі - разом з observation.manage_any). Архівоване спостереження приховується зі списків, медіафайли залишаються прикріпленими; також дозволяє бачити архівовані у списках (include_deleted)"),
    "role.view": ("Перегляд ролей", "Дозволяє переглядати інформацію про ролі та дозволи"),
    "role.create": ("Створення ролей", "Дозволяє створювати нові ролі та призначати дозволи"),
    "role.update": ("Оновлення ролей", "Дозволяє змінювати існуючі ролі та їх дозволи"),
    "role.archive": ("Архівування ролей", "Дозволяє архівувати та відновлювати ролі. Архівована роль одразу перестає надавати дозволи, її не можна призначати, але призначення користувачам зберігаються; також дозволяє бачити архівовані ролі у списках (include_deleted)"),
    "role.delete": ("Остаточне видалення ролей", "НЕБЕЗПЕЧНО: безповоротно видаляє архівовану роль без призначених користувачів разом з усіма її дозволами. Це єдина операція в системі, яка фізично видаляє дані; відновлення неможливе"),
    "admin.true": ("Адміністратор", "Повний доступ до системи з усіма адміністративними привілеями"),
    "audit.view": ("Перегляд аудиту", "Дозволяє переглядати системні журнали аудиту та історію активності"),
    "export.create": ("Створення експортів", "Дозволяє створювати та завантажувати експорти даних"),
    "category.view": ("Перегляд категорій", "Дозволяє переглядати інформацію про категорії"),
    "category.create": ("Створення категорій", "Дозволяє створювати нові категорії"),
    "category.update": ("Оновлення категорій", "Дозволяє змінювати існуючі категорії"),
    "category.archive": ("Архівування категорій", "Дозволяє архівувати та відновлювати категорії. Архівована категорія приховується зі списків, дані зберігаються; також дозволяє бачити архівовані у списках (include_deleted)"),
    "signal_type.view": ("Перегляд типів сигналів", "Дозволяє переглядати інформацію про типи сигналів"),
    "signal_type.create": ("Створення типів сигналів", "Дозволяє створювати нові типи сигналів"),
    "signal_type.update": ("Оновлення типів сигналів", "Дозволяє змінювати існуючі типи сигналів"),
    "signal_type.archive": ("Архівування типів сигналів", "Дозволяє архівувати та відновлювати типи сигналів. Тип з активними спостереженнями архівувати не можна; також дозволяє бачити архівовані у списках (include_deleted)"),
    "tag.view": ("Перегляд тегів", "Дозволяє переглядати інформацію про теги"),
    "tag.create": ("Створення тегів", "Дозволяє створювати нові теги"),
    "tag.update": ("Оновлення тегів", "Дозволяє змінювати існуючі теги"),
    "tag.archive": ("Архівування тегів", "Дозволяє архівувати та відновлювати теги. Архівований тег залишається прикріпленим до спостережень, але його не можна додавати до нових; також дозволяє бачити архівовані у списках (include_deleted)"),
    "media.view": ("Перегляд медіафайлів", "Дозволяє переглядати прикріплені медіафайли"),
    "media.create": ("Завантаження медіафайлів", "Дозволяє завантажувати нові медіафайли"),
    "media.update": ("Оновлення медіафайлів", "Дозволяє змінювати інформацію про медіафайли"),
    "media.archive": ("Архівування медіафайлів", "Дозволяє архівувати та відновлювати власні медіафайли (чужі - разом з media.manage_any). Архівований файл приховується зі спостереження, але залишається на диску"),
    "post.access_all": ("Доступ до всіх постів", "Дозволяє переглядати та працювати з даними всіх постів, а не лише призначених"),
    "observation.manage_any": ("Керування чужими спостереженнями", "Дозволяє змінювати, закривати, архівувати та відновлювати спостереження, створені іншими користувачами"),
    "media.manage_any": ("Керування чужими медіафайлами", "Дозволяє архівувати та відновлювати медіафайли, завантажені іншими користувачами"),
    "media_format.view": ("Перегляд форматів медіа", "Дозволяє переглядати перелік прийнятних форматів завантаження"),
    "media_format.create": ("Створення форматів медіа", "Дозволяє додавати прийнятні формати завантаження"),
    "media_format.update": ("Оновлення форматів медіа", "Дозволяє змінювати прийнятні формати та обмеження розміру файлів"),
    "media_format.archive": ("Архівування форматів медіа", "Дозволяє архівувати та відновлювати формати завантаження. Архівований формат одразу перестає прийматися, завантажені файли не змінюються; також дозволяє бачити архівовані у списках (include_deleted)"),
    "statistics.view": ("Перегляд статистики", "Дозволяє переглядати зведену статистику: адміністратор бачить усі пости, інші користувачі - лише доступні їм пости"),
}


async def seed_roles_and_permissions(session: AsyncSession):
    """Idempotent: ensure the admin role, every Permission row from the enum
    (display texts refreshed from PERMISSION_META), and that the admin role holds
    every permission (re-granted on every run). Renamed permissions are handled by
    migrations (010_archive_permissions), never here.

    Returns the admin Role.
    """
    from app.backend.models.role import Role, Permission, RolePermission
    from app.backend.enums.general import Permissions

    result = await session.exec(select(Role).where(Role.name == ADMIN_ROLE_NAME))
    admin_role = result.first()
    if not admin_role:
        admin_role = Role(
            name=ADMIN_ROLE_NAME,
            display_name="Administrator",
            description="Full system access"
        )
        session.add(admin_role)
        await session.flush()
        _logger.info("Admin role created")
    elif admin_role.is_deleted:
        admin_role.is_deleted = False
        session.add(admin_role)
        await session.flush()
        _logger.warning("Admin role was soft-deleted; restored by seeding")

    result = await session.exec(select(Permission))
    existing_permissions = {p.name: p for p in result.all()}

    permissions_added = 0
    permissions_refreshed = 0
    for perm in Permissions:
        display_name, description = PERMISSION_META.get(perm.value, (perm.value, ""))
        row = existing_permissions.get(perm.value)
        if row is None:
            session.add(Permission(name=perm.value, display_name=display_name, description=description))
            permissions_added += 1
        elif row.display_name != display_name or row.description != description:
            # Display texts are code-owned (there is no API to edit them), keep the database in sync
            row.display_name = display_name
            row.description = description
            session.add(row)
            permissions_refreshed += 1
    if permissions_added or permissions_refreshed:
        await session.flush()
        _logger.info(f"Seeded {permissions_added} new permissions, refreshed {permissions_refreshed} descriptions")

    # Re-grant: the admin role must hold every permission after every startup
    result = await session.exec(select(Permission.id))
    all_permission_ids = set(result.all())
    result = await session.exec(
        select(RolePermission.permission_id).where(RolePermission.role_id == admin_role.id)
    )
    granted_ids = set(result.all())
    missing_ids = sorted(all_permission_ids - granted_ids)
    for permission_id in missing_ids:
        session.add(RolePermission(role_id=admin_role.id, permission_id=permission_id))
    if missing_ids:
        _logger.info(f"Re-granted {len(missing_ids)} permissions to the admin role")

    await session.commit()
    return admin_role


async def seed_database():
    """Seed database with initial data (roles, permissions; admin user in dev only)"""
    # First ensure database exists (migrations should already be applied)
    await create_database_if_not_exists()

    _logger.info("Starting database seeding...")

    # Create initial admin user and role with proper PKI certificates
    session_maker = await get_session_maker()
    async with session_maker() as session:
        from app.backend.models.user import User
        from app.backend.models.role import Role, Permission, RolePermission, UserRole
        from app.backend.enums.general import Permissions
        from app.backend.utils.pki_generator import generate_admin_pki_credentials

        # 1. Seed roles and permissions (required in all modes)
        admin_role = await seed_roles_and_permissions(session)

        # 1b. Accepted upload formats (adds missing defaults, never edits existing rows)
        from app.backend.services.media.formats import seed_media_formats
        added_formats = await seed_media_formats(session)
        if added_formats:
            _logger.info(f"Seeded {added_formats} media format(s)")

        # 2. Seed Admin User (Development mode only)
        if settings.app.environment == AppEnvironment.DEVELOPMENT.value:
            # Check if admin exists
            result = await session.exec(
                select(User).where(User.username == settings.admin.username)
            )
            admin_user = result.first()

            if not admin_user:
                _logger.info("Seeding admin user in development mode...")
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
                        _logger.info("Generated new admin PKI credentials")

                        # Print certificate data in dev mode
                        if settings.app.debug:
                            _logger.debug("\n" + "=" * 80)
                            _logger.debug("GENERATED ADMIN PKI CREDENTIALS (DEV MODE)")
                            _logger.debug("=" * 80)
                            _logger.debug(f"CN:          {admin_pki['pki_certificate_cn']}")
                            _logger.debug(f"Serial:      {admin_pki['pki_certificate_serial']}")
                            _logger.debug(f"Fingerprint: {admin_pki['pki_certificate_fingerprint']}")
                            _logger.debug("=" * 80 + "\n")
                    except Exception as e:
                        _logger.error(f"Failed to generate PKI certificates: {e}")
                        # Don't raise here, maybe roles/perms seeding was enough
                        return

                # Create admin user
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
                user_role = UserRole(
                    user_id=admin_user.id,
                    role_id=admin_role.id
                )
                session.add(user_role)

                await session.commit()
                _logger.info(f"Admin user created successfully (mode: {'generated' if use_generated else 'configured'})")
            else:
                _logger.info("Admin user already exists")
        else:
            _logger.info("Skipping admin user seeding (not in development environment)")

    _logger.info("Database seeding process completed")



SCHEMA_LOCK_NAME = "watch_web_schema_init"
SCHEMA_LOCK_TIMEOUT_SECONDS = 300


class _SchemaLock:
    """MySQL advisory lock held while one process migrates and seeds.

    Every uvicorn worker runs the lifespan, so without this two workers run
    Alembic at the same time and MySQL rejects one with a concurrent DDL
    error. GET_LOCK is connection-scoped: the connection stays open until
    release. Other dialects (sqlite in tests) have no such lock and skip it.
    """

    def __init__(self):
        self._conn = None

    async def __aenter__(self):
        engine = await get_engine()
        if engine.dialect.name != "mysql":
            return self
        self._conn = await engine.connect()
        acquired = (await self._conn.execute(
            text("SELECT GET_LOCK(:name, :timeout)"),
            {"name": SCHEMA_LOCK_NAME, "timeout": SCHEMA_LOCK_TIMEOUT_SECONDS},
        )).scalar()
        if acquired != 1:
            await self._conn.close()
            self._conn = None
            raise RuntimeError(f"could not acquire schema lock {SCHEMA_LOCK_NAME} within {SCHEMA_LOCK_TIMEOUT_SECONDS}s")
        _logger.info("Schema lock acquired; running migrations and seeding")
        return self

    async def __aexit__(self, *exc):
        if self._conn is not None:
            try:
                await self._conn.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": SCHEMA_LOCK_NAME})
            finally:
                await self._conn.close()
                self._conn = None
            _logger.info("Schema lock released")


async def initialize_database_with_migrations():
    """Initialize database using migrations and then seed data, serialised across processes"""
    await create_database_if_not_exists()
    async with _SchemaLock():
        await _migrate_and_seed()


async def _migrate_and_seed():
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from alembic.config import Config
    from alembic import command

    _logger.info("Initializing database with migrations...")

    try:

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
