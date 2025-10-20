#!/usr/bin/env python3
"""Update admin PKI credentials to match config.toml"""

import asyncio
from app.backend.database import get_session
from app.backend.models.user import User
from app.backend.services.auth import AuthService
from app.backend.config import settings
from sqlmodel import select

async def update_admin_pki():
    try:
        print("Updating admin PKI credentials to match config.toml...")

        # Get admin user from database
        async for session in get_session():
            result = await session.exec(
                select(User).where(User.username == settings.admin.username)
            )
            admin_user = result.first()

            if not admin_user:
                print("[ERROR] Admin user not found in database")
                return

            print(f"[INFO] Found admin user: {admin_user.username}")

            # Hash the PKI data from config.toml
            config_cn_hash = AuthService.hash_pki_data(settings.admin.pki_certificate_cn)
            config_serial_hash = AuthService.hash_pki_data(settings.admin.pki_certificate_serial)
            config_fingerprint_hash = AuthService.hash_pki_data(settings.admin.pki_certificate_fingerprint)

            print(f"[INFO] Updating PKI hashes to:")
            print(f"   CN: {settings.admin.pki_certificate_cn}")
            print(f"   Serial: {settings.admin.pki_certificate_serial}")
            print(f"   Fingerprint: {settings.admin.pki_certificate_fingerprint}")

            # Update the admin user's PKI hashes
            admin_user.pki_certificate_cn_hash = config_cn_hash
            admin_user.pki_certificate_serial_hash = config_serial_hash
            admin_user.pki_certificate_fingerprint_hash = config_fingerprint_hash

            # Commit changes
            await session.commit()

            print("[OK] Admin PKI credentials updated successfully!")
            print("Authentication with config.toml values should now work.")

            break

    except Exception as e:
        print(f"[ERROR] Update failed: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(update_admin_pki())