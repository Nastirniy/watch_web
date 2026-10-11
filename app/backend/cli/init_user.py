#!/usr/bin/env python3
"""
Init User Utility - Create the first user from the server machine.

Usage:
    python -m app.backend.cli.init_user /path/to/cert.pem
    python -m app.backend.cli.init_user /path/to/cert.pem --username admin --alias "Admin" --role-ids 1,2
    python -m app.backend.cli.init_user --list-roles
"""

import asyncio
import sys
import argparse
from pathlib import Path

from loguru import logger

from app.backend.database import get_session_maker, create_database_if_not_exists
from app.backend.models.user import User
from app.backend.models.role import Role, UserRole
from app.backend.auth.certificates import (
    CN_SCHEME_CURRENT, CertificateParser, cn_hash_matches, hash_pki_certificate_data,
)
from app.backend.services.audit import create_audit_log

from sqlmodel import select


async def list_roles():
    """List all available roles in the database."""
    await create_database_if_not_exists()
    session_maker = await get_session_maker()

    async with session_maker() as session:
        result = await session.exec(select(Role).order_by(Role.id))
        roles = result.all()

        if not roles:
            logger.warning("No roles found in database. Run migrations and seed first:")
            logger.info("  python -m app.backend.cli.migrations_manager migrate")
            logger.info("  python -m app.backend.cli.migrations_manager seed")
            return []

        logger.info(f"Available roles ({len(roles)}):")
        for role in roles:
            logger.info(f"  ID: {role.id}  |  {role.name}  |  {role.display_name}")
        return roles


async def create_user(
    username: str,
    username_alias: str,
    cert_path: str,
    role_ids: list[int],
):
    """
    Create a user with PKI certificate authentication.

    Mirrors the logic from app/backend/api/v1/users.py create_user route.
    """
    await create_database_if_not_exists()
    session_maker = await get_session_maker()

    # Read certificate file
    cert_file = Path(cert_path)
    if not cert_file.exists():
        logger.error(f"Certificate file not found: {cert_path}")
        sys.exit(1)

    cert_data = cert_file.read_bytes()
    if len(cert_data) == 0:
        logger.error("Certificate file is empty")
        sys.exit(1)

    # Parse certificate
    try:
        cert_info = CertificateParser.parse_certificate(cert_data)
        pki_cn = cert_info["pki_certificate_cn"]
        pki_serial = cert_info["pki_certificate_serial"]
        pki_fingerprint = cert_info["pki_certificate_fingerprint"]
        logger.info("Certificate parsed OK:")
        logger.info(f"  CN:          {pki_cn}")
        logger.info(f"  Serial:      {pki_serial}")
        logger.info(f"  Fingerprint: {pki_fingerprint}")
    except ValueError as e:
        logger.error(f"Invalid certificate - {e}")
        sys.exit(1)

    # Hash PKI data
    pki_cn_hash, pki_serial_hash, pki_fingerprint_hash = await hash_pki_certificate_data(
        pki_cn, pki_serial, pki_fingerprint
    )

    async with session_maker() as session:
        # Check username uniqueness
        result = await session.exec(
            select(User).where(User.username == username)
        )
        if result.first():
            logger.error(f"User with username '{username}' already exists")
            sys.exit(1)

        # Check certificate uniqueness
        dup = await session.exec(
            select(User).where(
                await cn_hash_matches(pki_cn) |
                (User.pki_certificate_serial_hash == pki_serial_hash) |
                (User.pki_certificate_fingerprint_hash == pki_fingerprint_hash)
            )
        )
        if dup.first():
            logger.error("Certificate already registered to another user")
            sys.exit(1)

        # Validate roles
        roles_to_assign = []
        for role_id in role_ids:
            role_result = await session.exec(
                select(Role).where(Role.id == role_id)
            )
            role = role_result.first()
            if not role:
                logger.error(f"Role with ID {role_id} not found")
                sys.exit(1)
            roles_to_assign.append(role)

        # Create user
        user = User(
            username=username,
            username_alias=username_alias,
            pki_certificate_cn_hash=pki_cn_hash,
            pki_certificate_cn_hash_scheme=CN_SCHEME_CURRENT,
            pki_certificate_serial_hash=pki_serial_hash,
            pki_certificate_fingerprint_hash=pki_fingerprint_hash,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)

        # Assign roles
        for role in roles_to_assign:
            session.add(UserRole(user_id=user.id, role_id=role.id))
        await session.commit()

        # Create audit log
        await create_audit_log(
            session=session,
            action="create",
            resource_type="user",
            resource_id=user.id,
            new_values=user.model_dump(exclude={"pki_certificate_cn_hash", "pki_certificate_serial_hash", "pki_certificate_fingerprint_hash"}),
            details={"message": f"User {user.username} created via CLI init utility"},
            ip_address="127.0.0.1",
            request_url="CLI"
        )
        await session.commit()

        logger.success("User created successfully:")
        logger.info(f"  ID:    {user.id}")
        logger.info(f"  Name:  {user.username}")
        logger.info(f"  Alias: {user.username_alias}")
        logger.info(f"  Roles: {', '.join(r.name for r in roles_to_assign)}")


def _prompt(label: str, default: str = "") -> str:
    """Prompt for input with optional default."""
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or default


def _prompt_role_ids(roles) -> list[int]:
    """Prompt for role IDs after showing available roles."""
    role_ids_raw = input("Role IDs (comma-separated, e.g. 1,2): ").strip()
    if not role_ids_raw:
        logger.error("At least one role ID is required")
        sys.exit(1)
    try:
        return [int(x.strip()) for x in role_ids_raw.split(",") if x.strip()]
    except ValueError:
        logger.error("Role IDs must be integers")
        sys.exit(1)


async def interactive_create(cert_path: str = None):
    """Interactive mode -- prompts for missing fields."""
    logger.info("=== Create User ===")

    roles = await list_roles()
    if not roles:
        sys.exit(1)

    if not cert_path:
        cert_path = _prompt("Certificate file path (PEM or DER)")
        if not cert_path:
            logger.error("Certificate path is required")
            sys.exit(1)

    username = _prompt("Username (3-50 chars)")
    if not username or len(username) < 3 or len(username) > 50:
        logger.error("Username must be 3-50 characters")
        sys.exit(1)

    username_alias = _prompt("Display name")
    if not username_alias:
        logger.error("Display name is required")
        sys.exit(1)

    role_ids = _prompt_role_ids(roles)

    await create_user(username, username_alias, cert_path, role_ids)


def main():
    parser = argparse.ArgumentParser(
        description="Create initial user with PKI certificate authentication"
    )
    parser.add_argument("cert", nargs="?", default=None,
                        help="Path to PKI certificate file (PEM or DER)")
    parser.add_argument("--list-roles", action="store_true",
                        help="List available roles and exit")
    parser.add_argument("--username", type=str, help="Username (3-50 chars)")
    parser.add_argument("--alias", type=str, help="Display name")
    parser.add_argument("--role-ids", type=str,
                        help="Comma-separated role IDs, e.g. 1,2,3")

    args = parser.parse_args()

    if args.list_roles:
        asyncio.run(list_roles())
        return

    # If all flags provided -- run non-interactively
    if args.cert and args.username and args.alias and args.role_ids:
        try:
            role_ids = [int(x.strip()) for x in args.role_ids.split(",") if x.strip()]
        except ValueError:
            logger.error("--role-ids must be comma-separated integers (e.g. 1,2,3)")
            sys.exit(1)

        if len(args.username) < 3 or len(args.username) > 50:
            logger.error("Username must be 3-50 characters")
            sys.exit(1)

        asyncio.run(create_user(args.username, args.alias, args.cert, role_ids))
        return

    # Otherwise -- interactive, with cert pre-filled if given positionally
    asyncio.run(interactive_create(cert_path=args.cert))


if __name__ == "__main__":
    main()
