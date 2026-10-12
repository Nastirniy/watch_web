#!/usr/bin/env python3
"""
Init User Utility - Create the first user from the server machine.

Usage:
    python -m app.backend.cli.init_user /path/to/cert.pem
    python -m app.backend.cli.init_user /path/to/cert.pem --username admin --alias "Admin" --user-type admin
    python -m app.backend.cli.init_user --list-types

The user gets the built-in base role of the user type at the root of the scope tree (no home post); more access is
role assignments through the API.
"""

import asyncio
import sys
import argparse
from pathlib import Path

from loguru import logger

from app.backend.database import get_session_maker, create_database_if_not_exists
from app.backend.auth.scope import ROOT
from app.backend.auth.user_types import USER_TYPES
from app.backend.enums.general import UserType
from app.backend.models.user import User
from app.backend.models.role import Role
from app.backend.models.role_assignment import RoleAssignment, scope_columns
from app.backend.auth.certificates import (
    CN_SCHEME_CURRENT, CertificateParser, cn_hash_matches, hash_pki_certificate_data,
)
from app.backend.services.audit import create_audit_log

from sqlmodel import select


def list_types() -> None:
    """List the user types (the built-in role of each is its base role)."""
    logger.info(f"User types ({len(USER_TYPES)}):")
    for user_type, spec in USER_TYPES.items():
        logger.info(f"  {user_type.value:<16} {len(spec.base_permissions):>3} permissions")


async def create_user(
    username: str,
    username_alias: str,
    cert_path: str,
    user_type: UserType,
):
    """
    Create a user with PKI certificate authentication and the base role of the type at the root.

    Mirrors the logic from app/backend/api/v1/users.py create_user route (without a home post).
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

        # The base role of the type (seeding creates the built-in roles)
        role = (await session.exec(select(Role).where(Role.builtin_key == user_type.value))).first()
        if not role:
            logger.error(f"Built-in role '{user_type.value}' not found. Run migrations and seed first:")
            logger.info("  python -m app.backend.cli.migrations_manager migrate")
            logger.info("  python -m app.backend.cli.migrations_manager seed")
            sys.exit(1)

        # Create user
        user = User(
            username=username,
            username_alias=username_alias,
            pki_certificate_cn_hash=pki_cn_hash,
            pki_certificate_cn_hash_scheme=CN_SCHEME_CURRENT,
            pki_certificate_serial_hash=pki_serial_hash,
            pki_certificate_fingerprint_hash=pki_fingerprint_hash,
            user_type=user_type.value,
        )
        session.add(user)
        await session.flush()
        session.add(RoleAssignment(user_id=user.id, role_id=role.id, **scope_columns(ROOT)))
        await session.commit()
        await session.refresh(user)

        # Create audit log
        await create_audit_log(
            session=session,
            action="create",
            resource_type="user",
            resource_id=user.id,
            new_values={
                **user.model_dump(exclude={"pki_certificate_cn_hash", "pki_certificate_serial_hash", "pki_certificate_fingerprint_hash"}),
                "assignments": [{"role_id": role.id, "scope": ROOT.key}],
            },
            details={"message": f"User {user.username} created via CLI init utility"},
            ip_address="127.0.0.1",
            request_url="CLI"
        )
        await session.commit()

        logger.success("User created successfully:")
        logger.info(f"  ID:    {user.id}")
        logger.info(f"  Name:  {user.username}")
        logger.info(f"  Alias: {user.username_alias}")
        logger.info(f"  Type:  {user.user_type} ({role.name} at {ROOT.key})")


def _prompt(label: str, default: str = "") -> str:
    """Prompt for input with optional default."""
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or default


def _user_type(value: str) -> UserType:
    try:
        return UserType(value.strip())
    except ValueError:
        logger.error(f"Unknown user type '{value}': one of {', '.join(t.value for t in UserType)}")
        sys.exit(1)


async def interactive_create(cert_path: str = None):
    """Interactive mode -- prompts for missing fields."""
    logger.info("=== Create User ===")
    list_types()

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

    user_type = _user_type(_prompt("User type", UserType.ADMIN.value))

    await create_user(username, username_alias, cert_path, user_type)


def main():
    parser = argparse.ArgumentParser(
        description="Create initial user with PKI certificate authentication"
    )
    parser.add_argument("cert", nargs="?", default=None,
                        help="Path to PKI certificate file (PEM or DER)")
    parser.add_argument("--list-types", action="store_true",
                        help="List the user types and exit")
    parser.add_argument("--username", type=str, help="Username (3-50 chars)")
    parser.add_argument("--alias", type=str, help="Display name")
    parser.add_argument("--user-type", type=str, default=None,
                        help=f"User type: {', '.join(t.value for t in UserType)}")

    args = parser.parse_args()

    if args.list_types:
        list_types()
        return

    # If all flags provided -- run non-interactively
    if args.cert and args.username and args.alias and args.user_type:
        user_type = _user_type(args.user_type)

        if len(args.username) < 3 or len(args.username) > 50:
            logger.error("Username must be 3-50 characters")
            sys.exit(1)

        asyncio.run(create_user(args.username, args.alias, args.cert, user_type))
        return

    # Otherwise -- interactive, with cert pre-filled if given positionally
    asyncio.run(interactive_create(cert_path=args.cert))


if __name__ == "__main__":
    main()
