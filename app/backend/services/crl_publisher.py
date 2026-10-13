"""Publishing the CRL file nginx reads (spec section 7).

The API builds the list of revoked serials, the signer signs it, the API writes `settings.pki.crl_path` (the signed
CRL, then the CRLs parsed from the files of `extra_crl_paths`) and records the number and dates in
`certificate_authority_state`.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from cryptography.hazmat.primitives import serialization
from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.config import settings
from app.backend.enums.general import RevocationReason
from app.backend.models.user_certificate import CertificateAuthorityState, UserCertificate
from app.backend.pki.crl import CrlEntry, earliest_next_update, split_pem_crls
from app.backend.pki.store import write_atomically
from app.backend.services.signer_client import SignerUnavailable, signer_client
from app.backend.utils.datetime_utils import as_utc, assume_utc
from app.backend.utils.logging_config import get_security_logger

_logger = get_security_logger()

STATE_ID = 1


@dataclass(frozen=True)
class CrlStatus:
    this_update: Optional[datetime]
    next_update: Optional[datetime]
    revoked_count: int
    published_at: Optional[datetime]
    stale: bool                                  # a revocation the published file does not have
    earliest_next_update: Optional[datetime]     # the soonest next_update of every CRL in the file


async def _state(session: AsyncSession, lock: bool = False) -> CertificateAuthorityState:
    """The state row, inserted when missing (migration 024 inserts it; a schema made from the models has none)"""
    query = select(CertificateAuthorityState).where(CertificateAuthorityState.id == STATE_ID)
    if lock:
        query = query.with_for_update()
    state = (await session.exec(query)).first()
    if state is None:
        state = CertificateAuthorityState(id=STATE_ID)
        session.add(state)
        await session.flush()
    return state


def _is_due(state: CertificateAuthorityState, crl_path: Path, now: datetime) -> bool:
    """The file is missing, or half of its validity has passed"""
    if not crl_path.exists() or state.this_update is None or state.next_update is None:
        return True
    this_update, next_update = assume_utc(state.this_update), assume_utc(state.next_update)
    return now > this_update + (next_update - this_update) / 2


async def _entries(session: AsyncSession, now: datetime) -> list[CrlEntry]:
    """Revoked certificates of our client CA that have not expired (an expired one may leave the CRL, RFC 5280)"""
    rows = (await session.exec(
        select(UserCertificate)
        .where(UserCertificate.ca_issued.is_(True), UserCertificate.revoked_at.is_not(None),
               UserCertificate.serial.is_not(None))
        .where(UserCertificate.not_after.is_(None) | (UserCertificate.not_after >= as_utc(now)))
        .order_by(UserCertificate.id)
    )).all()
    return [CrlEntry(int(row.serial, 16), assume_utc(row.revoked_at),
                     RevocationReason(row.revocation_reason or RevocationReason.UNSPECIFIED.value))
            for row in rows]


class ExtraCrlError(Exception):
    """An extra CRL file is missing, unreadable or holds no parsable CRL"""


def _extra_crls() -> bytes:
    """The CRLs of every extra file, read and parsed now (at every publish) and written again as PEM: only CRLs reach
    the file nginx reads, never a comment or a damaged block. ExtraCrlError names the file that is not usable."""
    parts = []
    for path in settings.pki.extra_crl_paths:
        try:
            crls = split_pem_crls(Path(path).read_bytes())
        except (OSError, ValueError) as exc:
            raise ExtraCrlError(f"{path} is not a readable CRL file ({type(exc).__name__})") from None
        if not crls:
            raise ExtraCrlError(f"{path} holds no PEM CRL")
        parts += [crl.public_bytes(serialization.Encoding.PEM) for crl in crls]
    return b"".join(parts)


def _file_content(signed_pem: str, extra: bytes) -> bytes:
    """The signed CRL, then the extra CRLs"""
    signed = signed_pem.encode("ascii")
    return (signed if signed.endswith(b"\n") else signed + b"\n") + extra


async def publish_crl(session: AsyncSession, only_if_due: bool = False) -> bool:
    """Sign and write the CRL; True when the file was written.

    Locks the state row, so two API processes never overwrite a newer CRL with an older one. only_if_due (the
    start-up) re-checks inside the lock whether the file is still missing or past half of its validity. A signer
    that does not answer, or an extra CRL file that is missing or does not parse, rolls back (the number is not
    consumed, the published file stays as it is), is logged and gives False. Any other failure (SignerRefused, an
    OSError writing the file) rolls back as well and propagates: the revoke route and the start-up log it, the CLI
    fails. Without PKI settings nothing happens."""
    if not settings.pki.is_configured():
        return False
    crl_path = Path(settings.pki.crl_path)
    now = datetime.now(timezone.utc)
    state = await _state(session, lock=True)
    if only_if_due and not _is_due(state, crl_path, now):
        await session.rollback()
        return False
    try:
        extra = _extra_crls()
        entries = await _entries(session, now)
        number = state.crl_number + 1
        next_update = now + timedelta(days=settings.pki.crl_days)
        signed = await signer_client().sign_crl(number, now, next_update, entries)
        write_atomically(crl_path, _file_content(signed, extra))
    except ExtraCrlError as exc:
        await session.rollback()
        _logger.error(f"CRL not published: pki.extra_crl_paths: {exc}")
        return False
    except SignerUnavailable as exc:
        await session.rollback()
        _logger.warning(f"CRL not published: the signer does not answer ({exc})")
        return False
    except BaseException:
        # Cleanup on any other failure (the lock and the number are given back), then the error goes on
        await session.rollback()
        raise
    state.crl_number = number
    state.this_update, state.next_update = as_utc(now), as_utc(next_update)
    state.revoked_count = len(entries)
    state.published_at = as_utc(datetime.now(timezone.utc))
    session.add(state)
    await session.commit()
    _logger.info(f"CRL published: number={number}, revoked={len(entries)}")
    return True


async def crl_status(session: AsyncSession) -> CrlStatus:
    state = await _state(session)
    newest = (await session.exec(
        select(func.max(UserCertificate.revoked_at))
        .where(UserCertificate.ca_issued.is_(True), UserCertificate.revoked_at.is_not(None))
    )).one()
    published_at = assume_utc(state.published_at)
    stale = newest is not None and (published_at is None or assume_utc(newest) > published_at)
    earliest = None
    if settings.pki.crl_path:
        try:
            earliest = earliest_next_update(Path(settings.pki.crl_path).read_bytes())
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            # No usable CRL: without an earliest next_update /health is degraded
            _logger.error(f"The published CRL file cannot be read or parsed ({type(exc).__name__})")
    return CrlStatus(
        this_update=assume_utc(state.this_update), next_update=assume_utc(state.next_update),
        revoked_count=state.revoked_count, published_at=published_at, stale=stale, earliest_next_update=earliest)
