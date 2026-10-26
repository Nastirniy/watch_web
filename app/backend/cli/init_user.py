#!/usr/bin/env python3
"""
Init User Utility - Create the first user from the server machine.

Usage:
    python -m app.backend.cli.init_user /path/to/cert.pem
    python -m app.backend.cli.init_user /path/to/cert.pem --username admin --alias "Admin" --user-type system_administrator
    python -m app.backend.cli.init_user /path/to/cert.pem --username ivan --alias "Ivan" --user-type viewer --label "PC"
    python -m app.backend.cli.init_user --username admin --alias "Admin" --user-type system_administrator
    python -m app.backend.cli.init_user --list-types

The user gets the base roles of the user type: its tree base role at the root of the scope tree (no home post) and
its system base roles; more access is role assignments through the API. With a certificate file the user gets it as an uploaded certificate (label: --label,
default its CN); without one the user cannot log in until a certificate is issued or uploaded for them.
"""

import asyncio
import sys
import argparse
from pathlib import Path
from typing import Optional

from loguru import logger

from app.backend.database import close_db, get_session_maker, create_database_if_not_exists
from app.backend.auth.scope import ROOT
from app.backend.auth.user_types import USER_TYPES
from app.backend.enums.general import CertificateSource, RoleKind, UserType
from app.backend.errors.coded import CodedError
from app.backend.models.user import User
from app.backend.models.role import Role
from app.backend.models.role_assignment import RoleAssignment, scope_columns
from app.backend.models.system_role_assignment import SystemRoleAssignment
from app.backend.pki.profile import LABEL_MAX_LENGTH, normalize_label
from app.backend.services.audit import create_audit_log
from app.backend.services.certificates import register_certificate

from sqlmodel import select


def list_types() -> None:
    """List the user types with the number of permissions of their base roles."""
    logger.info(f"User types ({len(USER_TYPES)}):")
    for user_type, spec in USER_TYPES.items():
        logger.info(f"  {user_type.value:<16} {len(spec.base_permissions):>3} permissions")


async def create_user(
    username: str,
    username_alias: str,
    cert_path: Optional[str],
    user_type: UserType,
    label: Optional[str] = None,
):
    """
    Create a user with the base role of the type at the root and, with cert_path, an uploaded certificate (label:
    its readable name, default the CN).

    Mirrors the logic from app/backend/api/v1/users.py create_user route (without a home post).
    """
    await create_database_if_not_exists()
    session_maker = await get_session_maker()

    # Read certificate file
    cert_data = None
    if cert_path:
        cert_file = Path(cert_path)
        if not cert_file.exists():
            logger.error(f"Certificate file not found: {cert_path}")
            sys.exit(1)
        cert_data = cert_file.read_bytes()
        if len(cert_data) == 0:
            logger.error("Certificate file is empty")
            sys.exit(1)
    if label is not None:
        label = normalize_label(label)
        if label is None:
            logger.error(f"The label must be 1 to {LABEL_MAX_LENGTH} characters without control characters")
            sys.exit(1)

    async with session_maker() as session:
        # Check username uniqueness
        result = await session.exec(
            select(User).where(User.username == username)
        )
        if result.first():
            logger.error(f"User with username '{username}' already exists")
            sys.exit(1)

        # The base roles of the type (seeding creates the built-in roles), all at the root: the user has no home post
        spec = USER_TYPES[user_type]
        roles = []
        for base in spec.base_roles:
            role = (await session.exec(select(Role).where(Role.builtin_key == base.role.value))).first()
            if not role:
                logger.error(f"Built-in role '{base.role.value}' not found. Run migrations and seed first:")
                logger.info("  python -m app.backend.cli.migrations_manager migrate")
                logger.info("  python -m app.backend.cli.migrations_manager seed")
                sys.exit(1)
            roles.append(role)

        # Create user
        user = User(
            username=username,
            username_alias=username_alias,
            user_type=user_type.value,
        )
        session.add(user)
        await session.flush()
        for role in roles:
            if role.kind == RoleKind.SYSTEM.value:
                session.add(SystemRoleAssignment(user_id=user.id, role_id=role.id))
            else:
                session.add(RoleAssignment(user_id=user.id, role_id=role.id, **scope_columns(ROOT)))
        certificate = None
        if cert_data is not None:
            try:
                certificate = await register_certificate(
                    session, user, cert_data, label, CertificateSource.UPLOADED, None)
            except CodedError as exc:
                await session.rollback()
                logger.error(f"Certificate refused: {exc.code.title}")
                sys.exit(1)
            logger.info("Certificate registered:")
            logger.info(f"  Label:   {certificate.label}")
            logger.info(f"  CN:      {certificate.subject_cn}")
            logger.info(f"  Serial:  {certificate.serial}")
            logger.info(f"  Expires: {certificate.not_after}")
        certificate_id = certificate.id if certificate is not None else None
        await session.commit()
        await session.refresh(user)

        # Create audit log
        await create_audit_log(
            session=session,
            action="create",
            resource_type="user",
            resource_id=user.id,
            new_values={
                **user.model_dump(),
                "assignments": [{"role_id": role.id, "scope": ROOT.key}],
                "certificate_id": certificate_id,
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
        if certificate_id is None:
            logger.info("  No certificate: the user logs in once a certificate is issued or uploaded for them")


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


async def interactive_create(cert_path: str = None, label: Optional[str] = None):
    """Interactive mode -- prompts for missing fields."""
    logger.info("=== Create User ===")
    list_types()

    if not cert_path:
        cert_path = _prompt("Certificate file path (PEM or DER; empty: none)") or None

    username = _prompt("Username (3-50 chars)")
    if not username or len(username) < 3 or len(username) > 50:
        logger.error("Username must be 3-50 characters")
        sys.exit(1)

    username_alias = _prompt("Display name")
    if not username_alias:
        logger.error("Display name is required")
        sys.exit(1)

    user_type = _user_type(_prompt("User type", UserType.SYSTEM_ADMINISTRATOR.value))
    if cert_path and label is None:
        label = _prompt("Certificate label (empty: its CN)") or None

    await create_user(username, username_alias, cert_path, user_type, label)


async def _closing_pool(command):
    try:
        await command
    finally:
        # The pool's connections are closed while the event loop still runs (asyncio.run in main): left to the
        # garbage collector they were closed after it, and the command ended with "Event loop is closed"
        await close_db()


def main():
    parser = argparse.ArgumentParser(
        description="Create initial user, optionally with the certificate file it logs in with"
    )
    parser.add_argument("cert", nargs="?", default=None,
                        help="Path to a certificate file (PEM or DER); optional")
    parser.add_argument("--list-types", action="store_true",
                        help="List the user types and exit")
    parser.add_argument("--username", type=str, help="Username (3-50 chars)")
    parser.add_argument("--alias", type=str, help="Display name")
    parser.add_argument("--user-type", type=str, default=None,
                        help=f"User type: {', '.join(t.value for t in UserType)}")
    parser.add_argument("--label", type=str, default=None,
                        help=f"Readable name of the certificate, 1 to {LABEL_MAX_LENGTH} characters (default: its CN)")

    args = parser.parse_args()

    if args.list_types:
        list_types()
        return

    # If all flags provided -- run non-interactively (the certificate is optional)
    if args.username and args.alias and args.user_type:
        user_type = _user_type(args.user_type)

        if len(args.username) < 3 or len(args.username) > 50:
            logger.error("Username must be 3-50 characters")
            sys.exit(1)

        asyncio.run(_closing_pool(create_user(args.username, args.alias, args.cert, user_type, args.label)))
        return

    # Otherwise -- interactive, with cert pre-filled if given positionally
    asyncio.run(_closing_pool(interactive_create(cert_path=args.cert, label=args.label)))


if __name__ == "__main__":
    main()
