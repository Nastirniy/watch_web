"""Client certificates of users (table user_certificates): state, registration of a certificate file, lookups.

The state of a certificate is computed when it is read, never stored (spec section 4): revoked when revoked_at is
set, else expired when not_after is in the past, else active. Rows without not_after (legacy, configured) stay active
until revoked. certificate_state() computes it for a row, state_clause() is the same rule in SQL.

Services return rows and raise CodedError; the routes convert (ApiError.from_coded).
"""
import os
from datetime import datetime, timezone
from typing import Optional

from cryptography import x509
from sqlalchemy import bindparam
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.auth.certificates import CN_SCHEME_CURRENT, certificate_values, hash_certificate_data
from app.backend.config import settings
from app.backend.enums.errors import ErrorCode
from app.backend.enums.general import CertificateSource, CertificateState
from app.backend.errors.coded import CodedError
from app.backend.models.user import User
from app.backend.models.user_certificate import UserCertificate
from app.backend.pki.authority import is_signed_by, load_ca_certificate
from app.backend.pki.profile import LABEL_MAX_LENGTH
from app.backend.utils.datetime_utils import as_utc, assume_utc
from app.backend.utils.logging_config import get_security_logger
from app.backend.utils.text import fit_column

_logger = get_security_logger()

# The client CA certificate of settings.pki.ca_cert_path, read again when the path or the file's mtime changes
_ca_cache: Optional[tuple[str, float, x509.Certificate]] = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def certificate_state(row: UserCertificate, now: Optional[datetime] = None) -> CertificateState:
    if row.revoked_at is not None:
        return CertificateState.REVOKED
    if row.not_after is not None and assume_utc(row.not_after) < as_utc(now or _now()):
        return CertificateState.EXPIRED
    return CertificateState.ACTIVE


def state_clause(state: CertificateState, now: Optional[datetime] = None):
    """SQL condition of the rows in `state` (certificate_state in SQL). Without `now` the moment is taken when the
    statement runs."""
    moment = bindparam("certificate_now", callable_=_now) if now is None else as_utc(now)
    not_revoked = UserCertificate.revoked_at.is_(None)
    if state is CertificateState.REVOKED:
        return UserCertificate.revoked_at.is_not(None)
    if state is CertificateState.EXPIRED:
        return not_revoked & UserCertificate.not_after.is_not(None) & (UserCertificate.not_after < moment)
    return not_revoked & (UserCertificate.not_after.is_(None) | (UserCertificate.not_after >= moment))


def _client_ca() -> Optional[x509.Certificate]:
    """The client CA certificate, None without settings.pki.ca_cert_path. A file that cannot be read or is no CA
    certificate is a broken configuration (checked at startup): the error propagates."""
    global _ca_cache
    path = settings.pki.ca_cert_path
    if not path:
        return None
    mtime = os.stat(path).st_mtime
    if _ca_cache is None or _ca_cache[:2] != (path, mtime):
        _ca_cache = (path, mtime, load_ca_certificate(path))
    return _ca_cache[2]


def ca_issued(cert: x509.Certificate) -> bool:
    """Signed by our client CA (settings.pki.ca_cert_path): such a certificate goes on our CRL when revoked"""
    ca_cert = _client_ca()
    return ca_cert is not None and is_signed_by(cert, ca_cert)


async def register_certificate(
        session: AsyncSession,
        user: User,
        cert_data: bytes,
        label: Optional[str],
        source: CertificateSource,
        created_by: Optional[int],
) -> UserCertificate:
    """A certificate file (PEM or DER) as a row of the user: its plain values, the three hashes the login matches
    (scheme 2) and ca_issued. label is the readable name, already checked by the caller (pki.profile.normalize_label);
    without one the CN cut to LABEL_MAX_LENGTH. An expired file is accepted (its row is expired).

    Adds and flushes the row (its id is set), never commits. CodedError certificate_invalid when the file is no
    certificate, certificate_taken when the same certificate (fingerprint) is registered already, for any user."""
    try:
        values = certificate_values(cert_data)
    except ValueError as exc:
        _logger.warning(f"Certificate file not parsed for user_id={user.id}: {exc}")
        raise CodedError(ErrorCode.CERTIFICATE_INVALID) from None
    cn_hash, serial_hash, fingerprint_hash = await hash_certificate_data(values.cn, values.serial, values.fingerprint)
    taken = await session.exec(select(UserCertificate.id).where(UserCertificate.fingerprint_hash == fingerprint_hash))
    if taken.first() is not None:
        _logger.warning(f"Certificate already registered: CN={values.cn}, serial={values.serial}")
        raise CodedError(ErrorCode.CERTIFICATE_TAKEN)

    row = UserCertificate(
        user_id=user.id,
        source=source.value,
        label=label or values.cn.strip()[:LABEL_MAX_LENGTH],
        subject_cn=fit_column(UserCertificate.subject_cn, values.cn),
        serial=fit_column(UserCertificate.serial, values.serial),
        not_before=values.not_before,
        not_after=values.not_after,
        fingerprint_hash=fingerprint_hash,
        cn_hash=cn_hash,
        cn_hash_scheme=CN_SCHEME_CURRENT,
        serial_hash=serial_hash,
        ca_issued=ca_issued(values.certificate),
        created_by=created_by,
    )
    session.add(row)
    await session.flush()
    _logger.info(f"Certificate registered: id={row.id}, user_id={user.id}, source={source.value}, CN={values.cn}, "
                 f"serial={values.serial}, ca_issued={row.ca_issued}")
    return row


async def active_certificate(session: AsyncSession, user_id: int, fingerprint_hash: str) -> Optional[UserCertificate]:
    """The user's active certificate with this fingerprint hash (one indexed lookup), None when there is none"""
    result = await session.exec(select(UserCertificate).where(
        UserCertificate.fingerprint_hash == fingerprint_hash,
        UserCertificate.user_id == user_id,
        state_clause(CertificateState.ACTIVE),
    ))
    return result.first()
