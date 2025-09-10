#!/usr/bin/env python3
"""
Update admin user with new PKI credentials
"""

import asyncio
import sys
from pathlib import Path

# Add project root to Python path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from app.backend.config import settings
from app.backend.database import get_session_maker
from app.backend.models.user import User
from app.backend.services.auth import AuthService
from app.backend.utils.logging_config import setup_logging, get_logger
from sqlmodel import select

# Setup logging
setup_logging(
    log_file_path=None,
    log_level="INFO",
    environment="development"
)
logger = get_logger("update_admin")

async def update_admin_user():
    """Update admin user with new PKI credentials"""
    try:
        session_maker = await get_session_maker()
        async with session_maker() as session:
            # Find admin user
            result = await session.exec(
                select(User).where(User.username == settings.admin.username)
            )
            admin_user = result.first()
            
            if not admin_user:
                logger.error("Admin user not found!")
                return False
            
            logger.info(f"Found admin user: {admin_user.username} (ID: {admin_user.id})")
            
            # Update with new PKI credentials (hashed)
            admin_user.pki_certificate_cn_hash = AuthService.hash_pki_data(settings.admin.pki_certificate_cn)
            admin_user.pki_certificate_serial_hash = AuthService.hash_pki_data(settings.admin.pki_certificate_serial)
            admin_user.pki_certificate_fingerprint_hash = AuthService.hash_pki_data(settings.admin.pki_certificate_fingerprint)
            
            session.add(admin_user)
            await session.commit()
            
            logger.info(f"Admin user updated with new PKI credentials:")
            logger.info(f"  CN: {settings.admin.pki_certificate_cn}")
            logger.info(f"  Serial: {settings.admin.pki_certificate_serial}")
            logger.info(f"  Fingerprint: {settings.admin.pki_certificate_fingerprint}")
            
            return True
            
    except Exception as e:
        logger.error(f"Failed to update admin user: {e}")
        return False

if __name__ == "__main__":
    print("Updating admin user PKI credentials...")
    success = asyncio.run(update_admin_user())
    
    if success:
        print("Admin user updated successfully!")
        print("You can now login with:")
        print(f"  CN: {settings.admin.pki_certificate_cn}")
        print(f"  Serial: {settings.admin.pki_certificate_serial}")
        print(f"  Fingerprint: {settings.admin.pki_certificate_fingerprint}")
    else:
        print("Failed to update admin user")
        sys.exit(1)