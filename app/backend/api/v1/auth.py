from typing import Optional
from fastapi import APIRouter, Depends, Header, Request
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.database import get_session
from app.backend.auth.service import AuthService
from app.backend.auth.sessions import (
    begin_session_write, certificate_matches, claim_refresh, live_clause, live_session, session_state, touch,
)
from datetime import datetime, timezone
from sqlmodel import select
from app.backend.auth.require import require
from app.backend.auth.assignments import assignment_views
from app.backend.auth.context import AuthContext, load_auth_context
from app.backend.auth.require import Require
from app.backend.auth.route_access import route_access_map, routes_allowed_for
from app.backend.enums.errors import AuthErrorCode, ErrorCode, RequiredPermissions
from app.backend.models.user import User, UserSession
from app.backend.enums.routes import AuthRoutes
from app.backend.api.versioning import PublishedVersion, request_version
from app.backend.enums.general import AppEnvironment
from app.backend.utils.logging_config import get_auth_logger
from app.backend.config import settings
from app.backend.auth.certificates import parse_cn
from app.backend.services.certificates import session_certificate
from app.backend.api.v1.models.request.auth import (
    LoginRequest,
    RefreshRequest,
    PermissionCheckRequest,
)
from app.backend.api.v1.models.response.auth import (
    LoginResponse,
    LogoutResponse,
    UserInfoResponse,
    UserSessionsResponse,
    PermissionCheckResponse,
    RouteAccessResponse,
    RouteAccessEntryResponse,
    build_user_info,
    build_login_response,
    build_session_response,
)
from app.backend.errors.coded import CodedError
from app.backend.errors.problems import ApiError
from app.backend.errors.openapi import problems

router = APIRouter(prefix=AuthRoutes.BASE.value, tags=[AuthRoutes.tag()])
_logger = get_auth_logger()


@router.post(
    AuthRoutes.LOGIN.value,
    response_model=LoginResponse,
    responses=problems(
        ErrorCode.LOGIN_DATA_REQUIRED, AuthErrorCode.ACCOUNT_DEACTIVATED, AuthErrorCode.ACCOUNT_DELETED,
        AuthErrorCode.CERTIFICATE_DATA_MISSING, AuthErrorCode.CERTIFICATE_NOT_RECOGNIZED,
        AuthErrorCode.CERTIFICATE_REVOKED, AuthErrorCode.CERTIFICATE_VERIFICATION_FAILED,
    ),
)
async def login(
        request: Request,
        login_data: Optional[LoginRequest] = None,
        session: AsyncSession = Depends(get_session),
        # Set by nginx from the verified client certificate (production), never by the client
        x_ssl_client_verify: Optional[str] = Header(None, include_in_schema=False),
        x_ssl_client_dn: Optional[str] = Header(None, include_in_schema=False),
        x_ssl_client_fingerprint: Optional[str] = Header(None, include_in_schema=False),
        x_ssl_client_serial: Optional[str] = Header(None, include_in_schema=False),
):
    """Authenticate user with PKI certificate using SSL headers or JSON data for development"""
    client_ip = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent")

    # Check if we're in development mode and should use JSON data
    if settings.app.environment == AppEnvironment.DEVELOPMENT.value:
        if login_data:
            # Development mode: use JSON data from request body
            cn = login_data.certificate_cn
            ssl_serial = login_data.certificate_serial
            ssl_fingerprint = login_data.certificate_fingerprint

            _logger.info(f"Development mode: Using JSON authentication data from IP: {client_ip}")
        else:
            _logger.warning(f"Development mode: No authentication data provided from IP: {client_ip}")
            raise ApiError(ErrorCode.LOGIN_DATA_REQUIRED)

    else:
        # Production mode: extract SSL certificate data from headers
        ssl_verify = x_ssl_client_verify
        ssl_dn = x_ssl_client_dn
        ssl_fingerprint = x_ssl_client_fingerprint
        ssl_serial = x_ssl_client_serial

        # Verify that certificate was validated by nginx
        if ssl_verify != "SUCCESS":
            _logger.warning(
                f"Certificate verification failed from IP: {client_ip}, "
                f"status: {ssl_verify}"
            )
            raise ApiError(AuthErrorCode.CERTIFICATE_VERIFICATION_FAILED)

        # Parse CN from Distinguished Name
        cn = await parse_cn(ssl_dn)

        # Validate required certificate data
        if not cn or not ssl_serial or not ssl_fingerprint:
            _logger.warning(
                f"Missing certificate data from IP: {client_ip}, "
                f"CN: {cn}, Serial: {ssl_serial}, Fingerprint: {ssl_fingerprint}"
            )
            raise ApiError(AuthErrorCode.CERTIFICATE_DATA_MISSING)

    # Authenticate with enhanced PKI method
    user, auth_error = await AuthService.authenticate_pki(
        session,
        certificate_cn=cn,
        certificate_serial=ssl_serial,
        certificate_fingerprint=ssl_fingerprint,
        ip_address=client_ip
    )
    if not user:
        _logger.warning(
            f"Failed login attempt from IP: {client_ip}, "
            f"CN: {cn}, Serial: {ssl_serial}, error: {auth_error}"
        )

        raise ApiError(AuthErrorCode(auth_error or AuthErrorCode.CERTIFICATE_NOT_RECOGNIZED))

    # Create session
    # A production session is bound to the certificate nginx verified: every later request must present it
    # (it checks the account and the certificate again on the locked user row: a deactivation or a revocation that
    # landed after authenticate_pki answers as it would have there)
    development = settings.app.environment == AppEnvironment.DEVELOPMENT.value
    try:
        user_session = await AuthService.create_session(
            session,
            user,
            ip_address=client_ip,
            user_agent=user_agent,
            request_url=str(request.url),
            certificate_fingerprint=None if development else ssl_fingerprint,
        )
    except CodedError as exc:
        raise ApiError.from_coded(exc)

    # Both tokens are bound to the session
    access_token, refresh_token, expires_in = AuthService.session_tokens(user.id, user_session)

    _logger.info(
        f"Successful login for user {user.username} from IP: {client_ip}, "
        f"CN: {cn}, Serial: {ssl_serial}"
    )

    auth_context = await load_auth_context(session, user)
    return build_login_response(auth_context, await assignment_views(session, user.id), access_token, refresh_token,
                                expires_in)



@router.post(
    AuthRoutes.REFRESH.value,
    response_model=LoginResponse,
    responses=problems(
        AuthErrorCode.ACCOUNT_DEACTIVATED, AuthErrorCode.ACCOUNT_DELETED, AuthErrorCode.ACCOUNT_NOT_FOUND,
        AuthErrorCode.INVALID_REFRESH_TOKEN, AuthErrorCode.SESSION_EXPIRED, AuthErrorCode.TOKEN_CERTIFICATE_MISMATCH,
    ),
)
async def refresh_token(
        request: Request,
        refresh_request: RefreshRequest,
        session: AsyncSession = Depends(get_session),
        # Set by nginx from the verified client certificate (nginx-proxy.conf), never by the client
        x_ssl_client_fingerprint: Optional[str] = Header(None, include_in_schema=False),
):
    """Refresh access token using refresh token. A refresh token works once: the answer carries the next one, and
    the one presented is refused from then on (invalid_refresh_token; the session goes on with the newest)."""
    payload = AuthService.verify_token(refresh_request.refresh_token, "refresh")
    if not payload:
        raise ApiError(AuthErrorCode.INVALID_REFRESH_TOKEN)

    user_id = int(payload.get("sub"))
    session_id = payload.get("sid", None)
    presented_jti = payload.get("jti", None)

    # A refresh token always belongs to a session (POST /auth/login), which must still be live
    if not session_id or not presented_jti:
        raise ApiError(AuthErrorCode.INVALID_REFRESH_TOKEN)
    # The write begins with the shared user lock (sessions.begin_session_write): a refresh waits for an administrator
    # ending the user's sessions and then finds its session ended (session_expired). The first change retires the
    # token presented (sessions.claim_refresh); every check below that refuses the refresh rolls it back
    for attempt in (1, 2):
        await begin_session_write(session, user_id)
        next_jti = await claim_refresh(session, session_id, user_id, presented_jti)

        # Verify user still exists (as committed: the user lock keeps it so until the commit)
        user = (await session.exec(
            select(User).where(User.id == user_id).execution_options(populate_existing=True)
        )).first()
        if not user:
            raise ApiError(AuthErrorCode.ACCOUNT_NOT_FOUND)
        if user.is_active and not user.is_deleted:
            break

        # The refresh token of a deactivated or archived account is dead; end any session still live. The claim is
        # rolled back first and the end is a write of its own: inside the claim's transaction two refreshes of two
        # sessions of the user would each hold their own session row and wait for the other's (a deadlock, 1213)
        await session.rollback()
        ended = await AuthService.expire_sessions_of_inactive_user(session, user_id)
        # It locked the user row and put it, as committed, into `user`. Reactivated between the rollback and that
        # lock (nothing ended): the refresh runs again as for an active user, once
        if user.is_deleted or not user.is_active or attempt == 2:
            _logger.warning(f"Refresh attempt for inactive account: user_id={user_id}, sessions ended={ended}")
            if user.is_deleted:
                raise ApiError(AuthErrorCode.ACCOUNT_DELETED)
            raise ApiError(AuthErrorCode.ACCOUNT_DEACTIVATED)
        _logger.info(f"Account reactivated during a refresh, refreshing again: user_id={user_id}")

    refresh_session = await live_session(session, session_id, user_id, begin=False)   # the claim began the write
    if refresh_session is None:
        raise ApiError(AuthErrorCode.SESSION_EXPIRED)

    # A session bound to a certificate that is no active row of the user ends here (a login that raced a
    # revocation, an expired certificate)
    if await AuthService.end_session_of_revoked_certificate(session, refresh_session, user, begin=False):
        raise ApiError(AuthErrorCode.TOKEN_CERTIFICATE_MISMATCH)
    if not await certificate_matches(refresh_session, x_ssl_client_fingerprint):
        _logger.warning(f"Refresh token presented with another client certificate: session_id={session_id}, user_id={user_id}")
        raise ApiError(AuthErrorCode.TOKEN_CERTIFICATE_MISMATCH)
    if next_jti is None:
        # A copy of a refresh token that refreshed already (or lost a concurrent refresh): the session stays live
        _logger.warning(f"Refresh token used again: session_id={session_id}, user_id={user_id}")
        raise ApiError(AuthErrorCode.INVALID_REFRESH_TOKEN)
    # A refresh is activity: it moves the end of the session (never past the hard end, when one is set)
    touch(refresh_session)
    session.add(refresh_session)
    await session.commit()

    access_token, new_refresh_token, expires_in = AuthService.session_tokens(user.id, refresh_session)
    _logger.info(f"Token refreshed for user {user.username}")

    auth_context = await load_auth_context(session, user)
    return build_login_response(auth_context, await assignment_views(session, user.id), access_token,
                                new_refresh_token, expires_in)


@router.post(
    AuthRoutes.LOGOUT.value,
    response_model=LogoutResponse,
    responses=problems(ErrorCode.SESSION_ALREADY_INVALIDATED),
)
async def logout(
        request: Request,
        auth: AuthContext = Depends(require()),
        session: AsyncSession = Depends(get_session)
):
    """End the session of the access token (no body: the token names its session)"""
    current_user = auth.user

    success = await AuthService.invalidate_session(
        session,
        auth.session_id,
        current_user.id,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent")
    )

    if not success:
        _logger.warning(f"Session already invalidated for user {current_user.username}")
        raise ApiError(ErrorCode.SESSION_ALREADY_INVALIDATED)

    _logger.info(f"User {current_user.username} logged out successfully")
    return LogoutResponse(message="Logged out successfully")


@router.get(AuthRoutes.ME.value, response_model=UserInfoResponse)
async def get_current_user_info(
        auth: AuthContext = Depends(require()),
        session: AsyncSession = Depends(get_session)
):
    """The current user and what the web client builds its interface from: user_type (pages), permissions (menu),
    assignments, root_permissions and post_permissions (buttons at a post: root_permissions plus its entry), and
    certificate: the client certificate of this session (id, label, not_after, state; null for a session bound to
    none, the development login), so the web client can warn before it expires"""
    return build_user_info(auth, await assignment_views(session, auth.user.id),
                           await session_certificate(session, auth.user.id, auth.session_id))


@router.get(
    AuthRoutes.SESSIONS.value,
    response_model=UserSessionsResponse,
)
async def get_user_sessions(
        auth: AuthContext = Depends(require()),
        session: AsyncSession = Depends(get_session)
):
    """The caller's live sessions (online or idle), newest first"""
    rows = (await session.exec(
        select(UserSession)
        .where(UserSession.user_id == auth.user.id, live_clause())
        .order_by(UserSession.created_at.desc())
    )).all()
    now = datetime.now(timezone.utc)
    return UserSessionsResponse(sessions=[build_session_response(row, session_state(row, now)) for row in rows])




@router.post(AuthRoutes.CHECK_PERMISSION.value, response_model=PermissionCheckResponse)
async def check_permission(
        check: PermissionCheckRequest,
        auth: AuthContext = Depends(require())
):
    """Evaluate a permission requirement for the current user without calling the guarded route: held anywhere (what
    route reachability checks), or with post_id in effect at that post (an unknown post id: only the root counts)"""
    result = Require.of(all=check.all, any=check.any).evaluate(auth, post_id=check.post_id)
    return PermissionCheckResponse(allowed=result.allowed, missing=list(result.missing))


@router.get(AuthRoutes.ROUTE_ACCESS.value, response_model=RouteAccessResponse)
async def get_route_access(
        request: Request,
        auth: AuthContext = Depends(require()),
        version: PublishedVersion = Depends(request_version),
):
    """Routes of this API version the current user may call. Generated from route metadata; routes the user
    cannot call are not listed."""
    entries = [entry for entry in routes_allowed_for(route_access_map(request.app), auth) if version.owns(entry.path)]
    return RouteAccessResponse(routes=[
        RouteAccessEntryResponse(
            method=entry.method,
            path=entry.path,
            name=entry.name,
            tags=list(entry.tags),
            required=RequiredPermissions(**entry.require.as_dict())
        )
        for entry in entries
    ])
