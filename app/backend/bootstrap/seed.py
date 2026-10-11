"""Initial data, idempotent: roles and permissions, accepted media formats, the admin user in development."""
from sqlmodel import select

from app.backend.auth.certificates import hash_data
from app.backend.bootstrap.permissions import seed_roles_and_permissions
from app.backend.config import settings
from app.backend.database import create_database_if_not_exists, get_session_maker
from app.backend.enums.general import AppEnvironment
from app.backend.utils.logging_config import get_database_logger

_logger = get_database_logger()


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
