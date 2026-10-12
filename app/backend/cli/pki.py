#!/usr/bin/env python3
"""
Client certificate commands (run in the api container).

Usage:
    python -m app.backend.cli.pki crl      # publish the CRL now (the daily timer runs this)
    python -m app.backend.cli.pki status   # the client CA, the CRL, the signer, the certificates

Exit code 1: crl could not publish (the signer does not answer) or PKI is not configured.
"""
import argparse
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import func
from sqlmodel import select

from app.backend.config import settings
from app.backend.database import get_session_maker
from app.backend.enums.general import CertificateState
from app.backend.models.user_certificate import UserCertificate
from app.backend.pki.authority import CaCheckError, load_ca_certificate, sha256_fingerprint
from app.backend.services.certificates import state_clause
from app.backend.services.crl_publisher import crl_status, publish_crl
from app.backend.services.signer_client import SignerRefused, SignerUnavailable, signer_client

EXPIRING_DAYS = 30


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


async def run(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="pki", description="Client certificate commands")
    parser.add_argument("command", choices=["crl", "status"])
    args = parser.parse_args(argv)
    if not settings.pki.is_configured():
        print("PKI is not configured (MYSETTINGS_PKI__*)", file=sys.stderr)
        return 1
    session_maker = await get_session_maker()
    async with session_maker() as session:
        if args.command == "crl":
            if await publish_crl(session):
                print("CRL published")
                return 0
            print("CRL not published: the signer does not answer", file=sys.stderr)
            return 1
        return await _status(session)


def main(argv: Optional[list[str]] = None) -> int:
    return asyncio.run(run(argv or sys.argv[1:]))


if __name__ == "__main__":
    sys.exit(main())
