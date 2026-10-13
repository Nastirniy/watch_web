"""Client certificates of users (table user_certificates): state, registration of a certificate file, issuing and
re-downloading a package, lookups.

The state of a certificate is computed when it is read, never stored (spec section 4): revoked when revoked_at is
set, else expired when not_after is in the past, else active. Rows without not_after (legacy, configured) stay active
until revoked. certificate_state() computes it for a row, state_clause() is the same rule in SQL.

Issue, upload, export and revoke write their audit row through a callback (WriteAudit) inside their transaction: the route
adds who asked (client_info), the CLI "via: cli". What the row holds comes from certificate_audit_details: never a
key, a .p12 or a password.

Services return rows and raise CodedError; the routes convert (ApiError.from_coded).
"""
import asyncio
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, List, Optional

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from sqlalchemy import bindparam
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.auth.certificates import CN_SCHEME_CURRENT, certificate_values, hash_certificate_data
from app.backend.config import settings
from app.backend.enums.errors import ErrorCode
from app.backend.enums.general import CertificateSource, CertificateState, RevocationReason, SessionRevokeCause
from app.backend.errors.coded import CodedError
from app.backend.models.user import User, UserSession
from app.backend.models.user_certificate import UserCertificate
from app.backend.pki.authority import is_signed_by, load_ca_certificate, sha256_fingerprint
from app.backend.pki.package import build_p12, generate_key, new_password, p12_filename
from app.backend.pki.profile import LABEL_MAX_LENGTH
from app.backend.pki.store import (
    CertificateFiles, read_certificate, read_package, remove_certificate_dir, write_certificate_dir,
)
from app.backend.services.signer_client import SignerUnavailable, signer_client
from app.backend.utils.datetime_utils import as_utc, assume_utc
from app.backend.utils.logging_config import get_security_logger
from app.backend.utils.text import fit_column

_logger = get_security_logger()

# A certificate file larger than this is refused before it is parsed (POST /users/, the upload route)
MAX_CERTIFICATE_BYTES = 5 * 1024 * 1024

# Writes the audit row of a certificate inside the transaction of the change: the row and its
# certificate_audit_details
WriteAudit = Callable[[UserCertificate, dict], Awaitable[None]]


@dataclass(frozen=True)
class CertificatePackage:
    """An issued certificate and its package: what issuing and re-downloading answer"""
    certificate: UserCertificate
    p12: bytes = field(repr=False)      # secrets: never in a repr, so never in a log line or an exception text
    password: str = field(repr=False)
    filename: str

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


def certificate_audit_details(row: UserCertificate, certificate: Optional[x509.Certificate]) -> dict:
    """What an audit row of a certificate holds: label, serial, SHA-256 fingerprint and expiry; never a key, a .p12
    or a password. The fingerprint needs the certificate itself (a column holds only its hash) and not_after is the
    row's: without them (a legacy or configured row, an uploaded file that was not stored) they are left out, never
    made up."""
    details = {"label": row.label, "serial": row.serial}
    if certificate is not None:
        details["sha256_fingerprint"] = sha256_fingerprint(certificate)
    if row.not_after is not None:
        details["not_after"] = as_utc(row.not_after)
    return details


def _stored_certificate(row: UserCertificate) -> Optional[x509.Certificate]:
    """The certificate kept in the folder of an issued row; None for any other row, without a store_dir, or when the
    file is gone or damaged (logged: the audit row then has no fingerprint, the change still goes on)"""
    if not row.files_dir or not settings.pki.store_dir:
        return None
    try:
        return x509.load_pem_x509_certificate(read_certificate(Path(settings.pki.store_dir), row.files_dir))
    except (OSError, ValueError) as exc:
        _logger.warning(f"Certificate id={row.id}: its stored certificate {row.files_dir} is not readable "
                        f"({type(exc).__name__})")
        return None


async def get_user_certificate(session: AsyncSession, user_id: int, certificate_id: int,
                               lock: bool = False) -> UserCertificate:
    """The certificate of the user; CodedError certificate_not_found when there is none, also for the id of another
    user's certificate. lock: the row is locked (FOR UPDATE) and read again"""
    query = select(UserCertificate).where(UserCertificate.id == certificate_id, UserCertificate.user_id == user_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    row = (await session.exec(query)).first()
    if row is None:
        raise CodedError(ErrorCode.CERTIFICATE_NOT_FOUND)
    return row


async def user_certificates(session: AsyncSession, user_id: int,
                            state: Optional[CertificateState] = None) -> List[UserCertificate]:
    """Every certificate of the user (in `state` when given), oldest first"""
    query = select(UserCertificate).where(UserCertificate.user_id == user_id)
    if state is not None:
        query = query.where(state_clause(state))
    return list((await session.exec(query.order_by(UserCertificate.id))).all())


async def session_certificate(session: AsyncSession, user_id: int,
                              session_id: Optional[str]) -> Optional[UserCertificate]:
    """The certificate a login session is bound to (production login); None for a session bound to none
    (development login) or without a session id"""
    if not session_id:
        return None
    result = await session.exec(
        select(UserCertificate)
        .join(UserSession, UserSession.certificate_fingerprint_hash == UserCertificate.fingerprint_hash)
        .where(UserSession.id == session_id, UserSession.user_id == user_id, UserCertificate.user_id == user_id)
    )
    return result.first()


async def _lock_live_user(session: AsyncSession, user: User) -> User:
    """The user row locked (FOR UPDATE) and read again; CodedError user_not_found when it is archived. An inactive
    user may receive a certificate."""
    locked = (await session.exec(
        select(User).where(User.id == user.id).with_for_update().execution_options(populate_existing=True)
    )).first()
    if locked is None or locked.is_deleted:
        raise CodedError(ErrorCode.USER_NOT_FOUND)
    return locked


def _utc_text(value: Optional[datetime]) -> str:
    return "-" if value is None else as_utc(value).strftime("%Y-%m-%d %H:%M:%S UTC")


async def _issuer(session: AsyncSession, created_by: Optional[int]) -> str:
    if created_by is None:
        return "command line (pki issue)"
    issuer = await session.get(User, created_by)
    return f"{issuer.username} (user id {created_by})" if issuer is not None else f"user id {created_by}"


def _info_text(row: UserCertificate, user: User, issued_by: str) -> str:
    """info.txt of the folder, for browsing by hand (usernames change, the folder names are ids)"""
    return "".join(f"{name}: {value}\n" for name, value in (
        ("label", row.label),
        ("username", user.username),
        ("user id", user.id),
        ("certificate id", row.id),
        ("issued at", _utc_text(row.created_at)),
        ("issued by", issued_by),
        ("serial", row.serial),
        ("expires", _utc_text(row.not_after)),
    ))


async def issue_certificate(
        session: AsyncSession,
        user: User,
        label: str,
        validity_days: int,
        created_by: Optional[int],
        write_audit: WriteAudit,
) -> CertificatePackage:
    """Issue a certificate for the user in one transaction (spec section 5): lock the user row (archived: CodedError
    user_not_found), generate the key, have the signer sign it, insert the row (source issued, ca_issued), write
    the folder <store_dir>/<user_id>/<id>/, write the audit row (write_audit), commit.

    label is already checked (pki.profile.normalize_label), validity_days too (1..settings.pki.max_validity_days).
    CodedError certificate_authority_not_configured without PKI settings, certificate_authority_unavailable when
    the signer does not answer (nothing is written). On any failure after the signer answered, the session is
    rolled back, the folder removed and the error raised again."""
    user = await _lock_live_user(session, user)
    if not settings.pki.is_configured():
        raise CodedError(ErrorCode.CERTIFICATE_AUTHORITY_NOT_CONFIGURED)

    key = await asyncio.to_thread(generate_key, settings.pki.key_size)
    public_key_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo).decode("ascii")
    try:
        signed = await signer_client().sign_client(public_key_pem, label, validity_days)
    except SignerUnavailable as exc:
        _logger.warning(f"Certificate for user_id={user.id} not issued: the signer does not answer ({exc})")
        raise CodedError(ErrorCode.CERTIFICATE_AUTHORITY_UNAVAILABLE) from None
    certificate_pem = signed.certificate_pem.encode("ascii")
    certificate = x509.load_pem_x509_certificate(certificate_pem)
    password = new_password()
    p12 = await asyncio.to_thread(build_p12, key, certificate, label, password, settings.pki.p12_encryption)

    store_dir = Path(settings.pki.store_dir)
    files_dir: Optional[str] = None
    try:
        row = await register_certificate(session, user, certificate_pem, label, CertificateSource.ISSUED, created_by)
        row.ca_issued = True
        info = _info_text(row, user, await _issuer(session, created_by))
        files_dir = write_certificate_dir(store_dir, user.id, row.id, CertificateFiles(
            certificate_pem=certificate_pem, p12=p12, password=password, info=info))
        row.files_dir = files_dir
        session.add(row)
        await write_audit(row, certificate_audit_details(row, certificate))
        await session.commit()
    except BaseException:
        # Cleanup on any failure, then the error goes on (a failed audit row is a 500, never a certificate)
        try:
            await session.rollback()
        finally:
            if files_dir is not None:
                remove_certificate_dir(store_dir, files_dir)
        raise
    _logger.info(f"Certificate issued: id={row.id}, user_id={user.id}, serial={row.serial}, "
                 f"not_after={row.not_after}, created_by={created_by}")
    return CertificatePackage(certificate=row, p12=p12, password=password, filename=p12_filename(label, row.id))


async def upload_certificate(
        session: AsyncSession,
        user: User,
        cert_data: bytes,
        label: str,
        created_by: Optional[int],
        write_audit: WriteAudit,
) -> UserCertificate:
    """Register a certificate file for the user (source uploaded) in one transaction: lock the user row (archived:
    CodedError user_not_found), register_certificate (certificate_invalid, certificate_taken), the audit row,
    commit. The caller checked the size of the file and the label."""
    user = await _lock_live_user(session, user)
    row = await register_certificate(session, user, cert_data, label, CertificateSource.UPLOADED, created_by)
    await write_audit(row, certificate_audit_details(row, certificate_values(cert_data).certificate))
    await session.commit()
    return row


async def export_package(session: AsyncSession, certificate: UserCertificate,
                         write_audit: WriteAudit) -> CertificatePackage:
    """The stored package of an issued certificate again, from its folder (the signer is not needed):
    export_count + 1, last_exported_at, the audit row, commit. The caller locked the row
    (get_user_certificate(lock=True)).

    CodedError certificate_authority_not_configured without PKI settings, certificate_not_exportable for a
    certificate the API did not issue, certificate_not_active for a revoked or expired one,
    certificate_files_missing when its folder is gone."""
    if not settings.pki.is_configured():
        raise CodedError(ErrorCode.CERTIFICATE_AUTHORITY_NOT_CONFIGURED)
    if certificate.source != CertificateSource.ISSUED.value or not certificate.files_dir:
        raise CodedError(ErrorCode.CERTIFICATE_NOT_EXPORTABLE)
    if certificate_state(certificate) is not CertificateState.ACTIVE:
        raise CodedError(ErrorCode.CERTIFICATE_NOT_ACTIVE)
    store_dir = Path(settings.pki.store_dir)
    try:
        p12, password = read_package(store_dir, certificate.files_dir)
        certificate_pem = read_certificate(store_dir, certificate.files_dir)
    except FileNotFoundError:
        _logger.warning(f"Certificate id={certificate.id}: its folder {certificate.files_dir} is missing")
        raise CodedError(ErrorCode.CERTIFICATE_FILES_MISSING) from None

    certificate.export_count += 1
    certificate.last_exported_at = _now()
    session.add(certificate)
    await write_audit(certificate, certificate_audit_details(certificate, x509.load_pem_x509_certificate(certificate_pem)))
    await session.commit()
    _logger.info(f"Certificate exported: id={certificate.id}, user_id={certificate.user_id}, "
                 f"export_count={certificate.export_count}")
    return CertificatePackage(certificate=certificate, p12=p12, password=password,
                              filename=p12_filename(certificate.label, certificate.id))


async def revoke_certificate(
        session: AsyncSession,
        certificate: UserCertificate,
        reason: RevocationReason,
        revoked_by: int,
        write_audit: WriteAudit,
) -> UserCertificate:
    """Revoke a certificate (final): lock the row and read it again, CodedError certificate_already_revoked when it
    is revoked; set revoked_at, revoked_by and revocation_reason, end the live sessions bound to it, write the audit
    row (write_audit), commit. Everything lands in one transaction: a session of the revoked device never outlives
    the revocation. The CRL is published by the caller after the commit (crl_publisher.publish_crl)."""
    # auth.service imports this module (login looks certificates up): imported here to avoid the cycle
    from app.backend.auth.service import AuthService

    row = await get_user_certificate(session, certificate.user_id, certificate.id, lock=True)
    if row.revoked_at is not None:
        raise CodedError(ErrorCode.CERTIFICATE_ALREADY_REVOKED)
    row.revoked_at = _now()
    row.revoked_by = revoked_by
    row.revocation_reason = reason.value
    session.add(row)
    ended = await AuthService.invalidate_certificate_sessions(
        session, row.user_id, row.fingerprint_hash, SessionRevokeCause.CERTIFICATE_REVOKED,
        actor_user_id=revoked_by, commit=False)
    await write_audit(row, {**certificate_audit_details(row, _stored_certificate(row)), "reason": reason.value,
                            "sessions_ended": ended})
    await session.commit()
    _logger.info(f"Certificate revoked: id={row.id}, user_id={row.user_id}, serial={row.serial}, "
                 f"reason={reason.value}, revoked_by={revoked_by}, sessions_ended={ended}")
    return row
