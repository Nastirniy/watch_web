#!/usr/bin/env python3
"""
Client certificate commands (run in the api container).

Usage:
    python -m app.backend.cli.pki crl      # publish the CRL now (the daily timer runs this)
    python -m app.backend.cli.pki status   # the client CA, the CRL, the signer, the certificates
    python -m app.backend.cli.pki issue --username U --label L [--days N] --out DIR
        # issue a certificate for an existing user exactly like the API (folder, audit row with via: cli,
        # created_by null) and write <label>.p12 and password.txt into DIR (new or empty) to hand over

Exit code 1: crl could not publish (the signer does not answer), issue issued nothing, or PKI is not configured.
"""
import argparse
import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from sqlalchemy import func
from sqlmodel import select

from app.backend.config import settings
from app.backend.database import close_db, get_session_maker
from app.backend.enums.general import CertificateState
from app.backend.errors.coded import CodedError
from app.backend.models.user import User
from app.backend.models.user_certificate import UserCertificate
from app.backend.pki.authority import CaCheckError, load_ca_certificate, sha256_fingerprint
from app.backend.pki.profile import LABEL_MAX_LENGTH, normalize_label
from app.backend.services.audit import create_audit_log
from app.backend.services.certificates import issue_certificate, state_clause
from app.backend.services.crl_publisher import crl_status, publish_crl
from app.backend.services.signer_client import SignerRefused, SignerUnavailable, signer_client

EXPIRING_DAYS = 30
PASSWORD_FILE = "password.txt"


def _text(value: Optional[datetime]) -> str:
    return "never" if value is None else value.strftime("%Y-%m-%d %H:%M:%S UTC")


async def _count(session, *conditions) -> int:
    return (await session.exec(select(func.count(UserCertificate.id)).where(*conditions))).one()


async def _status(session) -> int:
    now = datetime.now(timezone.utc)
    try:
        ca = load_ca_certificate(settings.pki.ca_cert_path)
        print(f"client CA: {ca.subject.rfc4514_string()}")
        print(f"client CA expires: {_text(ca.not_valid_after_utc)} (sha256 {sha256_fingerprint(ca)})")
    except CaCheckError as exc:
        print(f"client CA: not usable ({exc.reason})")
    crl = await crl_status(session)
    print(f"CRL published: {_text(crl.published_at)}, valid until {_text(crl.next_update)}, "
          f"revoked listed {crl.revoked_count}")
    print(f"CRL file earliest next update: {_text(crl.earliest_next_update)}")
    print(f"CRL stale: {'yes' if crl.stale else 'no'}")
    try:
        await signer_client().health()
        print("signer: reachable")
    except (SignerUnavailable, SignerRefused) as exc:
        print(f"signer: not reachable ({type(exc).__name__})")
    active = state_clause(CertificateState.ACTIVE, now)
    print(f"certificates active: {await _count(session, active)}")
    print(f"certificates expiring within {EXPIRING_DAYS} days: "
          f"{await _count(session, active, UserCertificate.not_after.is_not(None), UserCertificate.not_after < now + timedelta(days=EXPIRING_DAYS))}")
    print(f"certificates revoked: {await _count(session, state_clause(CertificateState.REVOKED, now))}")
    return 0


def _write_private(path: Path, data: bytes) -> None:
    """A new file only the owner reads (0600); an existing one is never overwritten"""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(data)


async def _issue(session, args) -> int:
    """issue: the checks of the API's request (label, days), then issue_certificate like POST
    /users/{user_id}/certificates, without a caller"""
    label = normalize_label(args.label)
    if label is None:
        print(f"label must be 1 to {LABEL_MAX_LENGTH} characters without control characters", file=sys.stderr)
        return 1
    days = args.days if args.days is not None else settings.pki.validity_days
    if not 1 <= days <= settings.pki.max_validity_days:
        print(f"--days must be 1 to {settings.pki.max_validity_days}", file=sys.stderr)
        return 1
    out = Path(args.out)
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        print(f"{out} must be a new or empty directory (a package is never written over another)", file=sys.stderr)
        return 1
    user = (await session.exec(select(User).where(func.lower(User.username) == args.username.lower()))).first()
    if user is None:
        print(f"no user {args.username}", file=sys.stderr)
        return 1

    async def write_audit(row: UserCertificate, details: dict) -> None:
        await create_audit_log(
            session=session, action="issue", resource_type="user_certificate", resource_id=row.id, user_id=None,
            details={"via": "cli", "message": f"pki issue: certificate {row.id} of user {user.username}", **details},
        )

    try:
        package = await issue_certificate(session, user, label, days, None, write_audit)
    except CodedError as exc:
        print(f"certificate not issued: {exc.code.value}", file=sys.stderr)
        return 1
    row = package.certificate
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_private(out / package.filename, package.p12)
    _write_private(out / PASSWORD_FILE, package.password.encode("utf-8"))
    print(f"certificate {row.id} issued for {user.username}: serial {row.serial}, expires {_text(row.not_after)}")
    print(f"wrote {out / package.filename} and {out / PASSWORD_FILE}")
    return 0


async def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="pki", description="Client certificate commands")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("crl", help="publish the CRL now (the daily timer runs this)")
    commands.add_parser("status", help="the client CA, the CRL, the signer, the certificates")
    issue = commands.add_parser("issue", help="issue a certificate for an existing user and write the .p12 and "
                                              "password.txt into --out")
    issue.add_argument("--username", required=True, help="an existing, not archived user")
    issue.add_argument("--label", required=True,
                       help=f"readable name of the certificate (its CN), 1 to {LABEL_MAX_LENGTH} characters")
    issue.add_argument("--days", type=int, default=None,
                       help="validity in days (default settings.pki.validity_days, at most max_validity_days)")
    issue.add_argument("--out", required=True, help="a new or empty directory for <label>.p12 and password.txt")
    args = parser.parse_args(argv)
    if not settings.pki.is_configured():
        print("PKI is not configured (MYSETTINGS_PKI__*)", file=sys.stderr)
        return 1
    session_maker = await get_session_maker()
    try:
        async with session_maker() as session:
            if args.command == "crl":
                if await publish_crl(session):
                    print("CRL published")
                    return 0
                print("CRL not published: the signer does not answer", file=sys.stderr)
                return 1
            if args.command == "issue":
                return await _issue(session, args)
            return await _status(session)
    finally:
        # The pool's connections are closed while the event loop still runs (asyncio.run in main): left to the
        # garbage collector, aiomysql closes them after the loop and prints "Event loop is closed" to the journal
        await close_db()


def main(argv: Optional[list[str]] = None) -> int:
    return asyncio.run(run(argv or sys.argv[1:]))


if __name__ == "__main__":
    sys.exit(main())
