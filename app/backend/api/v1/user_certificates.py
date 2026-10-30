"""Client certificates of one user: list, issue, upload a file, one certificate, re-download the stored package,
revoke.

Checked in this order (scope first, then existence, then state), so that an outsider learns nothing about the
user's certificates: the user (404 user_not_found); user.view covering the user or the user themselves (403
user_self_only); an archived user only for callers with user.archive covering them (404 user_not_found);
certificate.view covering the user (403 permission_denied), the user themselves included; for a change also
check_target_user with the permission of the change (403 target_user_more_privileged); then the certificate (404
certificate_not_found, also for another user's certificate) and its state.

A write (issue, upload, export, revoke) first locks the user row (lock_certificate_owner) and runs every check after
the lock: every writer of a user's assignments locks the user first, so check_target_user cannot pass on assignments
another request is changing.
"""
from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, Form, Path, Query, Request, Response, status
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.api.shared.helpers.audit_helper import client_info
from app.backend.api.shared.helpers.db_errors import unique_violation_as
from app.backend.api.shared.openapi_docs import with_headers
from app.backend.api.v1.models.request.certificates import (
    CertificateIssueRequest, CertificateRevokeRequest, CertificateUploadForm,
)
from app.backend.api.v1.models.response.certificates import (
    PACKAGE_HEADERS, CertificatePackageResponse, CertificateResponse, certificate_to_response, package_to_response,
)
from app.backend.auth.access_control import AccessControlService
from app.backend.auth.context import AuthContext
from app.backend.auth.privileges import check_target_user
from app.backend.auth.require import require
from app.backend.config import settings
from app.backend.database import get_session
from app.backend.enums.errors import AccessErrorCode, ErrorCode
from app.backend.enums.general import CertificateState, Permissions
from app.backend.enums.media_types import ContentType
from app.backend.enums.routes import UserRoutes
from app.backend.errors.coded import CodedError
from app.backend.errors.openapi import GUARDED, problems
from app.backend.errors.problems import ApiError
from app.backend.models.user import User
from app.backend.models.user_certificate import UserCertificate
from app.backend.enums.audit import AuditEvent
from app.backend.services.audit import create_audit_log, home_scope
from app.backend.services.certificates import (
    MAX_CERTIFICATE_BYTES, WriteAudit, export_package, get_user_certificate, issue_certificate,
    lock_certificate_owner, revoke_certificate, upload_certificate, user_certificates,
)
from app.backend.services.crl_publisher import publish_crl
from app.backend.services.signer_client import SignerRefused
from app.backend.utils.logging_config import get_api_logger

router = APIRouter(prefix=UserRoutes.BASE.value, tags=[UserRoutes.tag()])
_logger = get_api_logger()

# The checks of every route (module docstring): the user, user.view, the archive, certificate.view
_OWNER_PROBLEMS = (ErrorCode.USER_NOT_FOUND, AccessErrorCode.USER_SELF_ONLY, *GUARDED)


async def _certificate_owner(session: AsyncSession, auth: AuthContext, user_id: int, lock: bool = False,
                             administrators: bool = False) -> User:
    """The user of the path, visible to the caller as GET /users/{user_id} shows it, with certificate.view covering
    them. lock for a write: the user row locked as the first statement of the write (lock_certificate_owner), so
    that check_target_user, which the write runs next, reads the user's assignments as they stand until the commit
    (every writer of a user's assignments locks the user first); administrators for a revocation: the
    administrators' lock before it."""
    if lock:
        user = await lock_certificate_owner(session, user_id, administrators=administrators)
    else:
        user = (await session.exec(select(User).where(User.id == user_id))).first()
    if not user:
        raise ApiError(ErrorCode.USER_NOT_FOUND)
    # Visibility before the state: an outsider gets the same 403 for an archived and a live user
    AccessControlService.check_user_view(auth, user)
    if user.is_deleted and not auth.covers_user(Permissions.USER_ARCHIVE, user):
        raise ApiError(ErrorCode.USER_NOT_FOUND)
    AccessControlService.check_user_permission(auth, Permissions.CERTIFICATE_VIEW, user)
    return user


def _audit(session: AsyncSession, request: Request, auth: AuthContext, event: AuditEvent, owner: User) -> WriteAudit:
    """The audit row of the change, written by the service inside its transaction"""
    async def write(row: UserCertificate, params: dict) -> None:
        await create_audit_log(
            session,
            event,
            params={"username": owner.username, **params},
            scope=home_scope(owner),
            resource_id=row.id,
            user_id=auth.user.id,
            **client_info(request, auth),
        )
    return write


@router.get(
    UserRoutes.USER_CERTIFICATES.value,
    response_model=List[CertificateResponse],
    responses=problems(*_OWNER_PROBLEMS),
)
async def list_user_certificates(
        user_id: Annotated[int, Path(ge=1)],
        state: Optional[CertificateState] = Query(default=None, description="Only certificates in this state"),
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.CERTIFICATE_VIEW])),
):
    """The certificates of a user, oldest first, revoked and expired ones included (state is computed when read).
    A user sees their own only with certificate.view covering them; GET /auth/me shows the certificate of the
    current session."""
    user = await _certificate_owner(session, auth, user_id)
    return [certificate_to_response(row) for row in await user_certificates(session, user.id, state)]


@router.post(
    UserRoutes.USER_CERTIFICATES.value,
    response_model=CertificatePackageResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        **with_headers(201, "The certificate and its package", PACKAGE_HEADERS),
        **problems(
            ErrorCode.CERTIFICATE_AUTHORITY_NOT_CONFIGURED, ErrorCode.CERTIFICATE_AUTHORITY_UNAVAILABLE,
            AccessErrorCode.TARGET_USER_MORE_PRIVILEGED, *_OWNER_PROBLEMS,
        ),
    },
)
async def issue_user_certificate(
        request: Request,
        response: Response,
        user_id: Annotated[int, Path(ge=1)],
        data: CertificateIssueRequest,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.CERTIFICATE_VIEW, Permissions.CERTIFICATE_CREATE])),
):
    """Issue a client certificate for the user: the server generates the key, the signing service signs the
    certificate (CN = label, clientAuth, validity_days from now), and the answer carries the .p12 and its password
    (Cache-Control: no-store). The package is stored on the server; certificate.export can download it again.

    Needs certificate.create covering the user and every assignment of the user grantable by the caller (403
    target_user_more_privileged). An archived user answers 404 user_not_found; an inactive user may receive one.
    503 certificate_authority_not_configured without PKI on the server, certificate_authority_unavailable when the
    signing service does not answer (nothing is issued)."""
    user = await _certificate_owner(session, auth, user_id, lock=True)
    await check_target_user(session, auth, user, Permissions.CERTIFICATE_CREATE)
    validity_days = data.validity_days if data.validity_days is not None else settings.pki.validity_days
    try:
        package = await issue_certificate(session, user, data.label, validity_days, auth.user.id,
                                          _audit(session, request, auth, AuditEvent.USER_CERTIFICATE_ISSUE, user))
    except CodedError as exc:
        raise ApiError.from_coded(exc)
    _logger.info(f"User {auth.user.username} issued certificate {package.certificate.id} for user {user.username}")
    response.headers["Cache-Control"] = "no-store"
    return package_to_response(package)


@router.post(
    UserRoutes.USER_CERTIFICATE_UPLOAD.value,
    response_model=CertificateResponse,
    status_code=status.HTTP_201_CREATED,
    responses=problems(
        ErrorCode.CERTIFICATE_FILE_EMPTY, ErrorCode.CERTIFICATE_FILE_TOO_LARGE, ErrorCode.CERTIFICATE_INVALID,
        ErrorCode.CERTIFICATE_TAKEN, AccessErrorCode.TARGET_USER_MORE_PRIVILEGED, *_OWNER_PROBLEMS,
    ),
)
async def upload_user_certificate(
        request: Request,
        user_id: Annotated[int, Path(ge=1)],
        # A form model holding a file: FastAPI would document it as urlencoded without the media type
        form: Annotated[CertificateUploadForm, Form(media_type=ContentType.MULTIPART_FORM_DATA.value)],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.CERTIFICATE_VIEW, Permissions.CERTIFICATE_CREATE])),
):
    """Register a certificate file (PEM or DER) the user logs in with (source uploaded), with its plain values and
    the hashes the login matches. ca_issued when it is signed by the client CA of this server. An expired file is
    accepted (listed as expired); the same certificate registered already, for any user, answers 400
    certificate_taken. The same checks as issuing; an archived user answers 404 user_not_found."""
    user = await _certificate_owner(session, auth, user_id, lock=True)
    await check_target_user(session, auth, user, Permissions.CERTIFICATE_CREATE)
    cert_data = await form.file.read(MAX_CERTIFICATE_BYTES + 1)
    if len(cert_data) == 0:
        raise ApiError(ErrorCode.CERTIFICATE_FILE_EMPTY)
    if len(cert_data) > MAX_CERTIFICATE_BYTES:
        raise ApiError(ErrorCode.CERTIFICATE_FILE_TOO_LARGE, max_bytes=MAX_CERTIFICATE_BYTES)
    try:
        # Two uploads of the same certificate pass the SELECT check together: the unique index decides
        async with unique_violation_as(session, ErrorCode.CERTIFICATE_TAKEN):
            row = await upload_certificate(session, user, cert_data, form.label, auth.user.id,
                                           _audit(session, request, auth, AuditEvent.USER_CERTIFICATE_UPLOAD, user))
    except CodedError as exc:
        raise ApiError.from_coded(exc)
    _logger.info(f"User {auth.user.username} uploaded certificate {row.id} for user {user.username}")
    return certificate_to_response(row)


@router.get(
    UserRoutes.USER_CERTIFICATE_BY_ID.value,
    response_model=CertificateResponse,
    responses=problems(ErrorCode.CERTIFICATE_NOT_FOUND, *_OWNER_PROBLEMS),
)
async def get_user_certificate_by_id(
        user_id: Annotated[int, Path(ge=1)],
        certificate_id: Annotated[int, Path(ge=1)],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.CERTIFICATE_VIEW])),
):
    """One certificate of the user; a certificate of another user answers 404 certificate_not_found"""
    user = await _certificate_owner(session, auth, user_id)
    try:
        row = await get_user_certificate(session, user.id, certificate_id)
    except CodedError as exc:
        raise ApiError.from_coded(exc)
    return certificate_to_response(row)


@router.post(
    UserRoutes.USER_CERTIFICATE_EXPORT.value,
    response_model=CertificatePackageResponse,
    responses={
        **with_headers(200, "The certificate and its stored package", PACKAGE_HEADERS),
        **problems(
            ErrorCode.CERTIFICATE_NOT_FOUND, ErrorCode.CERTIFICATE_AUTHORITY_NOT_CONFIGURED,
            ErrorCode.CERTIFICATE_NOT_EXPORTABLE, ErrorCode.CERTIFICATE_NOT_ACTIVE, ErrorCode.CERTIFICATE_FILES_MISSING,
            AccessErrorCode.TARGET_USER_MORE_PRIVILEGED, *_OWNER_PROBLEMS,
        ),
    },
)
async def export_user_certificate(
        request: Request,
        response: Response,
        user_id: Annotated[int, Path(ge=1)],
        certificate_id: Annotated[int, Path(ge=1)],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.CERTIFICATE_VIEW, Permissions.CERTIFICATE_EXPORT])),
):
    """Download the stored package of an issued certificate again (the same .p12 and password as when it was
    issued; Cache-Control: no-store). POST because every download is counted (export_count, last_exported_at) and
    audited. certificate.export is a system permission (held at the root); the caller must also be able to grant
    every assignment of the user (403 target_user_more_privileged).

    409 certificate_not_exportable for a certificate this server did not issue, certificate_not_active for a revoked
    or expired one; 410 certificate_files_missing when its folder was removed; 503
    certificate_authority_not_configured without PKI on the server. The signing service is not needed."""
    user = await _certificate_owner(session, auth, user_id, lock=True)
    await check_target_user(session, auth, user, Permissions.CERTIFICATE_EXPORT)
    try:
        row = await get_user_certificate(session, user.id, certificate_id, lock=True)
        package = await export_package(
            session, row, _audit(session, request, auth, AuditEvent.USER_CERTIFICATE_EXPORT, user))
    except CodedError as exc:
        raise ApiError.from_coded(exc)
    _logger.info(f"User {auth.user.username} downloaded certificate {row.id} of user {user.username} again")
    response.headers["Cache-Control"] = "no-store"
    return package_to_response(package)


@router.post(
    UserRoutes.USER_CERTIFICATE_REVOKE.value,
    response_model=CertificateResponse,
    responses=problems(
        ErrorCode.CERTIFICATE_NOT_FOUND, ErrorCode.CERTIFICATE_ALREADY_REVOKED, ErrorCode.LAST_ADMIN_REQUIRED,
        AccessErrorCode.TARGET_USER_MORE_PRIVILEGED, *_OWNER_PROBLEMS,
    ),
)
async def revoke_user_certificate(
        request: Request,
        user_id: Annotated[int, Path(ge=1)],
        certificate_id: Annotated[int, Path(ge=1)],
        data: Optional[CertificateRevokeRequest] = None,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.CERTIFICATE_VIEW, Permissions.CERTIFICATE_REVOKE])),
):
    """Revoke a certificate for good: the login with it is refused (401 certificate_revoked) and the live sessions
    bound to it end in the same transaction (the user's other devices keep working). A certificate this server issued
    goes on the CRL, published after the commit; when that publication fails (the signing service does not answer,
    for example) the revocation stands, the answer is still 200 and the next publication (daily, at start-up) lists
    it (GET /health shows a stale CRL).

    Needs certificate.revoke covering the user and every assignment of the user grantable by the caller (403
    target_user_more_privileged). 409 certificate_already_revoked for a revoked one. The body may be left out
    (reason unspecified).

    409 last_admin_required: where login requires a client certificate (every environment but development) one
    effective administrator must remain: an active, not archived user holding the built-in system_administrator
    system role and administrator at the root, whose user type puts both in effect, with an active certificate. The
    last active certificate of the last one cannot be revoked."""
    user = await _certificate_owner(session, auth, user_id, lock=True, administrators=True)
    await check_target_user(session, auth, user, Permissions.CERTIFICATE_REVOKE)
    reason = (data or CertificateRevokeRequest()).reason
    try:
        row = await revoke_certificate(
            session, await get_user_certificate(session, user.id, certificate_id), reason, auth.user.id,
            _audit(session, request, auth, AuditEvent.USER_CERTIFICATE_REVOKE, user))
    except CodedError as exc:
        raise ApiError.from_coded(exc)
    _logger.info(f"User {auth.user.username} revoked certificate {row.id} of user {user.username} ({reason.value})")
    result = certificate_to_response(row)
    # Read now: publish_crl rolls the session back when it does not publish, which expires the row
    revoked_id, publishes = row.id, row.ca_issued
    if publishes and settings.pki.is_configured():
        # The revocation is committed: a failed publication only leaves the CRL stale (GET /health), never an error
        # answer
        try:
            published = await publish_crl(session)
        except SignerRefused as exc:
            _logger.error(f"CRL not published after revoking certificate {revoked_id}: the signer refused ({exc})")
        except OSError as exc:
            _logger.error(f"CRL not published after revoking certificate {revoked_id}: the file cannot be written "
                          f"({exc!r})")
        else:
            if not published:
                _logger.warning(f"CRL not published after revoking certificate {revoked_id}: it stays stale")
    return result
