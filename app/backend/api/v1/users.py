from typing import List, Optional, Annotated, Tuple
from fastapi import APIRouter, Depends, Form, Request, Query, Path
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError, DataError
from sqlmodel import select
from datetime import datetime, timezone

from app.backend.api.shared.helpers.db_errors import is_data_too_long, is_unique_violation
from app.backend.api.shared.helpers.post_helper import refuse_archived_post
from app.backend.database import get_session
from app.backend.models.user import User, UserSession
from app.backend.models.role import Role
from app.backend.models.role_assignment import RoleAssignment, scope_columns
from app.backend.models.post import Post
from app.backend.auth.context import AuthContext
from app.backend.auth.require import require
from app.backend.auth.access_control import AccessControlService
from app.backend.auth.privileges import (
    check_admin_kept, check_an_admin_remains, check_grant_at, check_target_user, role_permission_names,
)
from app.backend.auth.scope import Node, ROOT
from app.backend.bootstrap.permissions import role_level
from app.backend.auth.service import AuthService
from app.backend.auth.certificates import (
    CN_SCHEME_CURRENT, CertificateParser, cn_hash_matches, hash_pki_certificate_data,
)
from app.backend.enums.general import (
    PermissionLevel, Permissions, SessionEndReason, SessionRevokeCause, SessionStatus, UserType,
)
from app.backend.auth import sessions as session_lifetimes
from app.backend.api.v1.models.response.auth import SessionsEndedResponse, UserSessionResponse, build_session_response
from app.backend.enums.routes import UserRoutes
from app.backend.api.shared.helpers.audit_helper import client_info
from app.backend.services.audit import create_audit_log
from app.backend.utils.logging_config import get_api_logger
from app.backend.api.v1.models.request.users import UserCreateForm, UserUpdateForm
from app.backend.api.v1.models.response.users import UserResponse
from app.backend.api.v1.models.query.users import UserFilterParams
from app.backend.filters.user_filter import UserFilterService
from app.backend.errors.problems import ApiError
from app.backend.enums.errors import AccessErrorCode, ErrorCode
from app.backend.errors.openapi import GUARDED, problems

router = APIRouter(prefix=UserRoutes.BASE.value, tags=[UserRoutes.tag()])
_logger = get_api_logger()


def taken_error_code(exc: IntegrityError) -> ErrorCode | None:
    """The problem for a unique violation on users.username or a certificate hash column, otherwise None.

    e.orig holds only the database message (sqlite: "users.username", MySQL: the key name, which contains the column);
    str(exc) also carries the SQL, which names every column, so it is never searched.
    """
    if not is_unique_violation(exc):
        return None
    message = str(exc.orig)
    if "username" in message:
        return ErrorCode.USERNAME_TAKEN
    if "pki_certificate" in message:
        return ErrorCode.CERTIFICATE_TAKEN
    return None

# PATCH /users/{id} reads form fields only; a JSON body would reach the route with every field unset
FORM_CONTENT_TYPES = ("multipart/form-data", "application/x-www-form-urlencoded")
MAX_CERTIFICATE_BYTES = 5 * 1024 * 1024


async def _default_assignment(
        session: AsyncSession, auth: AuthContext, user_type: UserType, post_id: Optional[int],
) -> Tuple[Role, Node]:
    """The first assignment of a new user: the built-in base role of the type at the home post, at the root without
    one or when the base role holds system permissions (admin). The caller needs user.create covering that node
    (403 permission_required_for_action) and every permission of the role there (403
    grant_exceeds_own_permissions)."""
    role = (await session.exec(select(Role).where(Role.builtin_key == user_type.value))).one()
    permissions = await role_permission_names(session, [role.id])
    node = ROOT if post_id is None or role_level(permissions) is PermissionLevel.SYSTEM else Node.post(post_id)
    AccessControlService.check_covers(auth, Permissions.USER_CREATE, node, "create_user")
    check_grant_at(auth, permissions, node)
    return role, node


def _username_taken(username: str, exclude_user_id: Optional[int] = None):
    """Case-insensitive lookup: "Ivan" and "ivan" are the same username"""
    query = select(User).where(func.lower(User.username) == username.lower())
    if exclude_user_id is not None:
        query = query.where(User.id != exclude_user_id)
    return query


@router.get(
    UserRoutes.ROOT.value,
    response_model=List[UserResponse],
    responses=problems(
        AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION,
        AccessErrorCode.POST_ACCESS_DENIED, *GUARDED,
    ),
)
async def list_users(
        filters: Annotated[UserFilterParams, Query()],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.USER_VIEW]))
):
    """
    List users with filters.

    The users whose home post is where user.view is in effect, and the caller; users without a home post only
    with user.view at the root. Archived users only with include_deleted (requires user.archive), and only where
    user.archive is in effect.
    """
    current_user = auth.user
    archived = AccessControlService.archived_scope(auth, filters, Permissions.USER_ARCHIVE)
    scope = AccessControlService.resolve_scope(auth, Permissions.USER_VIEW, filters.post_id)

    query = select(User)
    query = UserFilterService.apply_filters(query, filters, scope, archived_post_ids=archived, caller_id=current_user.id)
    result = await session.exec(query)
    users = result.all()

    _logger.info(f"User {current_user.username} listed {len(users)} users (scope={sorted(scope) if scope is not None else 'all'})")

    return [UserResponse.model_validate(user) for user in users]


@router.post(
    UserRoutes.ROOT.value,
    response_model=UserResponse,
    responses=problems(
        ErrorCode.CERTIFICATE_FILE_EMPTY, ErrorCode.CERTIFICATE_INVALID, ErrorCode.CERTIFICATE_TAKEN,
        ErrorCode.USERNAME_OR_CERTIFICATE_TAKEN, ErrorCode.USERNAME_TAKEN, ErrorCode.POST_NOT_FOUND,
        ErrorCode.POST_ARCHIVED, ErrorCode.CERTIFICATE_FILE_TOO_LARGE, ErrorCode.DATA_TOO_LONG,
        AccessErrorCode.GRANT_EXCEEDS_OWN_PERMISSIONS, AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION, *GUARDED,
    ),
)
async def create_user(
        request: Request,
        user_data: Annotated[UserCreateForm, Form()],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.USER_VIEW, Permissions.USER_CREATE]))
):
    """
    Create new user with PKI certificate authentication

    The certificate file will be parsed to extract:
    - Common Name (CN)
    - Serial Number
    - SHA256 Fingerprint

    These fields are hashed before storage for security.

    The form fields are described by UserCreateForm. The user gets one assignment: the built-in base role of
    user_type at the home post (post_id), or at the root without a home post or when the base role holds system
    permissions (admin). The caller needs user.create covering that node (403 permission_required_for_action) and
    every permission of the role there (403 grant_exceeds_own_permissions). These checks answer first: a post_id
    outside the caller's scope gets the same 403 whether the post exists, is archived or not. An unknown post answers
    404 post_not_found and an archived post 409 post_archived (it cannot be the home post).
    """
    current_user = auth.user
    certificate = user_data.certificate
    username = user_data.username

    # Scope before the post: an outsider gets the same 403 for an unknown, an archived and a live post outside it
    base_role, node = await _default_assignment(session, auth, user_data.user_type, user_data.post_id)
    if user_data.post_id is not None:
        post = (await session.exec(select(Post).where(Post.id == user_data.post_id))).first()
        if not post:
            raise ApiError(ErrorCode.POST_NOT_FOUND, post_id=user_data.post_id)
        refuse_archived_post(post)

    # Check if user already exists
    result = await session.exec(_username_taken(user_data.username))
    if result.first():
        raise ApiError(ErrorCode.USERNAME_TAKEN)

    # Read and parse certificate
    try:
        cert_data = await certificate.read()
        if len(cert_data) == 0:
            raise ApiError(ErrorCode.CERTIFICATE_FILE_EMPTY)

        if len(cert_data) > MAX_CERTIFICATE_BYTES:
            raise ApiError(ErrorCode.CERTIFICATE_FILE_TOO_LARGE, max_bytes=MAX_CERTIFICATE_BYTES)

        # Parse certificate and extract PKI fields
        cert_info = CertificateParser.parse_certificate(cert_data)
        pki_cn = cert_info["pki_certificate_cn"]
        pki_serial = cert_info["pki_certificate_serial"]
        pki_fingerprint = cert_info["pki_certificate_fingerprint"]

        _logger.info(f"Certificate parsed successfully for user {username}: CN={pki_cn}, Serial={pki_serial[:16]}...")

    except ValueError as e:
        _logger.error(f"Certificate parsing error for user {username}: {str(e)}")
        raise ApiError(ErrorCode.CERTIFICATE_INVALID)

    # Hash PKI certificate data before storing
    pki_cn_hash, pki_serial_hash, pki_fingerprint_hash = await hash_pki_certificate_data(
        pki_cn, pki_serial, pki_fingerprint
    )

    # Check for duplicate certificate before creating user
    duplicate_check = await session.exec(
        select(User).where(
            await cn_hash_matches(pki_cn) |
            (User.pki_certificate_serial_hash == pki_serial_hash) |
            (User.pki_certificate_fingerprint_hash == pki_fingerprint_hash)
        )
    )
    if duplicate_check.first():
        raise ApiError(ErrorCode.CERTIFICATE_TAKEN)

    # Create user with validated data and hashed PKI data
    user = User(
        username=user_data.username,
        username_alias=user_data.username_alias,
        post_id=user_data.post_id,
        user_type=user_data.user_type.value,
        pki_certificate_cn_hash=pki_cn_hash,
        pki_certificate_cn_hash_scheme=CN_SCHEME_CURRENT,
        pki_certificate_serial_hash=pki_serial_hash,
        pki_certificate_fingerprint_hash=pki_fingerprint_hash
    )
    session.add(user)

    try:
        # The user and its assignment are written by one commit: a user never exists without an assignment
        await session.flush()
        session.add(RoleAssignment(user_id=user.id, role_id=base_role.id, created_by=current_user.id,
                                   **scope_columns(node)))
        await session.commit()
        await session.refresh(user)

        # Create audit log
        await create_audit_log(
            session=session,
            action="create",
            resource_type="user",
            resource_id=user.id,
            user_id=current_user.id,
            new_values={
                **user.model_dump(exclude={"pki_certificate_cn_hash", "pki_certificate_serial_hash", "pki_certificate_fingerprint_hash"}),
                "assignments": [{"role_id": base_role.id, "scope": node.key}],
            },
            details={"message": f"User {current_user.username} created new user {user.username} "
                                f"({user.user_type}, {base_role.name} at {node.key})"},
            **client_info(request, auth)
        )
        await session.commit()
        
        _logger.info(f"User {current_user.username} created user {user_data.username} ({user.user_type}) with PKI authentication")

        return UserResponse(
            id=user.id,
            username=user.username,
            username_alias=user.username_alias,
            post_id=user.post_id,
            is_active=user.is_active,
            created_at=user.created_at,
            updated_at=user.updated_at,
            last_login_at=user.last_login_at
        )
    except (IntegrityError, DataError) as e:
        await session.rollback()
        if is_data_too_long(e):
            raise ApiError(ErrorCode.DATA_TOO_LONG)
        if isinstance(e, IntegrityError) and is_unique_violation(e):
            raise ApiError(ErrorCode.USERNAME_OR_CERTIFICATE_TAKEN)
        raise


@router.get(
    UserRoutes.USER_BY_ID.value,
    response_model=UserResponse,
    responses=problems(AccessErrorCode.USER_SELF_ONLY, ErrorCode.USER_NOT_FOUND, *GUARDED),
)
async def get_user(
        user_id: Annotated[int, Path(ge=1)],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.USER_VIEW]))
):
    """Get user by ID - visible to the user itself and to callers with user.view in effect at the user's home post
    (at the root for a user without one); an archived user also needs user.archive there"""

    result = await session.exec(select(User).where(User.id == user_id))
    user = result.first()
    if not user:
        raise ApiError(ErrorCode.USER_NOT_FOUND)

    # Visibility before the state: an outsider gets the same 403 for an archived and a live user
    AccessControlService.check_user_view(auth, user)

    # Archived users are for those who manage the archive where the user belongs
    if user.is_deleted and not auth.covers_user(Permissions.USER_ARCHIVE, user):
        raise ApiError(ErrorCode.USER_NOT_FOUND)

    return UserResponse.model_validate(user)


@router.patch(
    UserRoutes.USER_BY_ID.value,
    response_model=UserResponse,
    responses=problems(
        ErrorCode.CANNOT_DEACTIVATE_SELF, ErrorCode.CERTIFICATE_FILE_EMPTY, ErrorCode.CERTIFICATE_INVALID,
        ErrorCode.CERTIFICATE_TAKEN, ErrorCode.USERNAME_TAKEN, ErrorCode.POST_NOT_FOUND, ErrorCode.POST_ARCHIVED,
        ErrorCode.USER_NOT_FOUND, ErrorCode.CERTIFICATE_FILE_TOO_LARGE, ErrorCode.FORM_CONTENT_TYPE_REQUIRED,
        ErrorCode.LAST_ADMIN_REQUIRED, ErrorCode.CANNOT_REMOVE_OWN_ADMIN,
        AccessErrorCode.TARGET_USER_MORE_PRIVILEGED, AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION,
        *GUARDED,
    ),
)
async def update_user(
        request: Request,
        user_id: Annotated[int, Path(ge=1)],
        update_data: Annotated[UserUpdateForm, Form()],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.USER_VIEW, Permissions.USER_UPDATE]))
):
    """
    Update user fields - all fields are optional

    Supports updating:
    - username: Unique username (3-50 characters)
    - username_alias: User's display name (1-255 characters)
    - post_id: Home post (membership: who manages the user; it grants nothing by itself). Moving the user needs
      user.update covering the new post too (403 permission_required_for_action, the same for an unknown or
      archived post outside the caller's scope); inside it an unknown post answers 404 post_not_found and an
      archived one 409 post_archived (sending the current home post again changes nothing and is accepted)
    - is_active: User's active status (boolean)
    - certificate: Update PKI certificate authentication data (file upload). A different certificate ends every
      session of the user, like deactivation: the old certificate's tokens stop working.

    Only provided fields will be updated. Archived users answer 404 until restored (POST /{user_id}/restore).
    The fields are form fields: any other body (JSON included) answers 415 instead of being ignored.

    Changing a user needs user.update covering them (their home post, the root for a user without one) and, for
    another user, that the caller could grant every assignment they have (403 target_user_more_privileged). A
    deactivation must leave an active holder of the built-in admin role at the root (409 last_admin_required,
    cannot_remove_own_admin).
    """
    current_user = auth.user
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if content_type and content_type not in FORM_CONTENT_TYPES:
        raise ApiError(
            ErrorCode.FORM_CONTENT_TYPE_REQUIRED,
            headers={"Accept-Patch": ", ".join(FORM_CONTENT_TYPES)},
            content_type=content_type,
        )

    # Find user
    result = await session.exec(
        select(User).where(User.id == user_id)
    )
    user = result.first()

    if not user:
        raise ApiError(ErrorCode.USER_NOT_FOUND)
    if user.is_deleted:
        raise ApiError(ErrorCode.USER_NOT_FOUND)
    await check_target_user(session, auth, user, Permissions.USER_UPDATE)

    # Store old values for auditing
    old_values = user.model_dump(exclude={"pki_certificate_cn_hash", "pki_certificate_serial_hash", "pki_certificate_fingerprint_hash"})

    certificate = update_data.certificate
    if update_data.username is not None:
        existing_username = await session.exec(_username_taken(update_data.username, exclude_user_id=user_id))
        if existing_username.first():
            raise ApiError(ErrorCode.USERNAME_TAKEN)
        user.username = update_data.username

    if update_data.post_id is not None:
        moving = update_data.post_id != user.post_id
        if moving:
            # A move: user.update must cover the new home post as well as the old one (check_target_user). Scope
            # before the post: an outsider gets the same 403 for an unknown, an archived and a live post outside it
            AccessControlService.check_covers(auth, Permissions.USER_UPDATE, Node.post(update_data.post_id), "move_user")
        post = (await session.exec(select(Post).where(Post.id == update_data.post_id))).first()
        if not post:
            raise ApiError(ErrorCode.POST_NOT_FOUND, post_id=update_data.post_id)
        if moving:
            refuse_archived_post(post)
        user.post_id = update_data.post_id

    if update_data.username_alias is not None:
        user.username_alias = update_data.username_alias

    deactivating = False
    if update_data.is_active is not None:
        if update_data.is_active is False and user.id == current_user.id:
            raise ApiError(ErrorCode.CANNOT_DEACTIVATE_SELF)
        deactivating = user.is_active and not update_data.is_active
        user.is_active = update_data.is_active

    # Handle certificate update if provided
    certificate_replaced = False
    if certificate is not None:
        try:
            cert_data = await certificate.read()
            if len(cert_data) == 0:
                raise ApiError(ErrorCode.CERTIFICATE_FILE_EMPTY)

            if len(cert_data) > MAX_CERTIFICATE_BYTES:
                raise ApiError(ErrorCode.CERTIFICATE_FILE_TOO_LARGE, max_bytes=MAX_CERTIFICATE_BYTES)

            # Parse certificate and extract PKI fields
            cert_info = CertificateParser.parse_certificate(cert_data)
            pki_cn = cert_info["pki_certificate_cn"]
            pki_serial = cert_info["pki_certificate_serial"]
            pki_fingerprint = cert_info["pki_certificate_fingerprint"]

            _logger.info(f"Certificate parsed for user {user.username}: CN={pki_cn}, Serial={pki_serial[:16]}...")

            # Hash PKI certificate data before storing
            pki_cn_hash, pki_serial_hash, pki_fingerprint_hash = await hash_pki_certificate_data(
                pki_cn, pki_serial, pki_fingerprint
            )

            # Check for duplicate certificate (excluding current user)
            duplicate_check = await session.exec(
                select(User).where(
                    (
                        await cn_hash_matches(pki_cn) |
                        (User.pki_certificate_serial_hash == pki_serial_hash) |
                        (User.pki_certificate_fingerprint_hash == pki_fingerprint_hash)
                    ) &
                    (User.id != user_id)
                )
            )
            if duplicate_check.first():
                raise ApiError(ErrorCode.CERTIFICATE_TAKEN)

            certificate_replaced = user.pki_certificate_fingerprint_hash != pki_fingerprint_hash
            user.pki_certificate_cn_hash = pki_cn_hash
            user.pki_certificate_cn_hash_scheme = CN_SCHEME_CURRENT
            user.pki_certificate_serial_hash = pki_serial_hash
            user.pki_certificate_fingerprint_hash = pki_fingerprint_hash

            _logger.info(f"User {current_user.username} updated certificate for user {user.username}")

        except ValueError as e:
            _logger.error(f"Certificate parsing error for user {user.username}: {str(e)}")
            raise ApiError(ErrorCode.CERTIFICATE_INVALID)

    # Update timestamp
    user.updated_at = datetime.now(timezone.utc)
    session.add(user)

    # Deactivation and a new certificate end every live session in the same transaction as the change, so the
    # user's tokens stop working the moment the update is committed (the request dependency rejects them).
    # A certificate is replaced because the old one is lost, compromised or revoked: its sessions must not
    # outlive it. An administrator replacing their own certificate ends their own session too.
    revoke_cause = (SessionRevokeCause.ACCOUNT_DEACTIVATED if deactivating
                    else SessionRevokeCause.CERTIFICATE_CHANGED if certificate_replaced else None)
    if revoke_cause:
        invalidated_count = await AuthService.invalidate_all_user_sessions(
            session=session,
            user_id=user_id,
            cause=revoke_cause,
            actor_user_id=current_user.id,
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            commit=False
        )
        _logger.info(
            f"User {current_user.username} updated user {user.username} ({revoke_cause.value}), "
            f"invalidated {invalidated_count} active sessions"
        )

    try:
        if deactivating:
            # A change that may take the admin role at the root from its last holder
            await check_admin_kept(session, auth)

        await session.commit()

        await session.refresh(user)
        
        # Create audit log
        await create_audit_log(
            session=session,
            action="update",
            resource_type="user",
            resource_id=user.id,
            user_id=current_user.id,
            old_values=old_values,
            new_values={
                **user.model_dump(exclude={"pki_certificate_cn_hash", "pki_certificate_serial_hash", "pki_certificate_fingerprint_hash"}),
                "certificate_replaced": certificate_replaced,
            },
            details={"message": f"User {current_user.username} updated user {user.username}"},
            **client_info(request, auth)
        )
        await session.commit()
        
        _logger.info(f"User {current_user.username} updated user {user.username}")

        return UserResponse.model_validate(user)

    except IntegrityError as e:
        await session.rollback()
        # A concurrent request took the username or the certificate after the checks above
        code = taken_error_code(e)
        if code is None:
            raise
        raise ApiError(code)


async def _get_active_user_or_404(session: AsyncSession, user_id: int) -> User:
    result = await session.exec(select(User).where(User.id == user_id, User.is_deleted == False))
    user = result.first()
    if not user:
        raise ApiError(ErrorCode.USER_NOT_FOUND)
    return user


@router.post(
    UserRoutes.USER_ARCHIVE.value,
    response_model=UserResponse,
    responses=problems(
        ErrorCode.CANNOT_ARCHIVE_SELF, ErrorCode.USER_NOT_FOUND, ErrorCode.LAST_ADMIN_REQUIRED,
        AccessErrorCode.TARGET_USER_MORE_PRIVILEGED, *GUARDED,
    ),
)
async def archive_user(
        request: Request,
        user_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.USER_VIEW, Permissions.USER_ARCHIVE]))
):
    """Archive a user (is_deleted=true): deactivates the account and invalidates its sessions.

    Nothing is removed: observations, media and role assignments of the user stay. The user is hidden from
    lists unless include_deleted=true. Undo with POST /{user_id}/restore. Already archived or unknown: 404.
    Needs user.archive covering the user and every assignment of the user grantable by the caller (403
    target_user_more_privileged); the last administrator: 409.
    """
    current_user = auth.user
    user = await _get_active_user_or_404(session, user_id)
    if user.id == current_user.id:
        raise ApiError(ErrorCode.CANNOT_ARCHIVE_SELF)
    await check_target_user(session, auth, user, Permissions.USER_ARCHIVE)

    old_values = user.model_dump(exclude={"pki_certificate_cn_hash", "pki_certificate_serial_hash", "pki_certificate_fingerprint_hash"})
    user.is_deleted = True
    user.is_active = False
    user.updated_at = datetime.now(timezone.utc)
    session.add(user)
    await check_an_admin_remains(session)

    invalidated = await AuthService.invalidate_all_user_sessions(
        session=session,
        user_id=user_id,
        cause=SessionRevokeCause.ACCOUNT_ARCHIVED,
        actor_user_id=current_user.id,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        commit=False,
    )
    await create_audit_log(
        session=session,
        action="archive",
        resource_type="user",
        resource_id=user.id,
        user_id=current_user.id,
        old_values=old_values,
        details={"message": f"User {current_user.username} archived user {user.username} ({invalidated} sessions invalidated)"},
        **client_info(request, auth)
    )
    await session.commit()
    await session.refresh(user)
    _logger.info(f"User {current_user.username} archived user {user.username}")
    return UserResponse.model_validate(user)


@router.post(
    UserRoutes.USER_RESTORE.value,
    response_model=UserResponse,
    responses=problems(
        ErrorCode.USER_NOT_FOUND, ErrorCode.USER_NOT_ARCHIVED, AccessErrorCode.TARGET_USER_MORE_PRIVILEGED, *GUARDED,
    ),
)
async def restore_user(
        request: Request,
        user_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.USER_VIEW, Permissions.USER_ARCHIVE]))
):
    """Restore an archived user (is_deleted=false, is_active=true): the account can log in again. Not archived: 409.
    Needs user.archive covering the user and every assignment of the user grantable by the caller (403
    target_user_more_privileged)."""
    current_user = auth.user
    result = await session.exec(select(User).where(User.id == user_id))
    user = result.first()
    if not user:
        raise ApiError(ErrorCode.USER_NOT_FOUND)
    if not user.is_deleted:
        raise ApiError(ErrorCode.USER_NOT_ARCHIVED)
    await check_target_user(session, auth, user, Permissions.USER_ARCHIVE)

    old_values = user.model_dump(exclude={"pki_certificate_cn_hash", "pki_certificate_serial_hash", "pki_certificate_fingerprint_hash"})
    user.is_deleted = False
    user.is_active = True
    user.updated_at = datetime.now(timezone.utc)
    session.add(user)
    await create_audit_log(
        session=session,
        action="restore",
        resource_type="user",
        resource_id=user.id,
        user_id=current_user.id,
        old_values=old_values,
        details={"message": f"User {current_user.username} restored user {user.username}"},
        **client_info(request, auth)
    )
    await session.commit()
    await session.refresh(user)
    _logger.info(f"User {current_user.username} restored user {user.username}")
    return UserResponse.model_validate(user)


@router.get(
    UserRoutes.USER_SESSIONS.value,
    response_model=List[UserSessionResponse],
    responses=problems(ErrorCode.USER_NOT_FOUND, AccessErrorCode.USER_SELF_ONLY, *GUARDED),
)
async def list_user_sessions(
        user_id: Annotated[int, Path(ge=1)],
        include_ended: bool = Query(False, description="Also list ended sessions, with end_reason and revoke_cause"),
        limit: int = Query(50, ge=1, le=500, description="Newest first"),
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.USER_VIEW]))
):
    """Login sessions of a user, newest first. status is computed when read: online (a request within
    security.online_threshold_seconds), idle (live, client quiet) or ended; a session past its absolute end or
    idle timeout is ended even if nothing has recorded it yet. user.view must cover the user (403 user_self_only
    otherwise); an archived user, like GET /{user_id}, only for callers with user.archive covering them (404
    otherwise)."""
    target = (await session.exec(select(User).where(User.id == user_id))).first()
    if not target:
        raise ApiError(ErrorCode.USER_NOT_FOUND)
    # Visibility before the state: an outsider gets the same 403 for an archived and a live user
    AccessControlService.check_user_view(auth, target)
    if target.is_deleted and not auth.covers_user(Permissions.USER_ARCHIVE, target):
        raise ApiError(ErrorCode.USER_NOT_FOUND)
    query = select(UserSession).where(UserSession.user_id == user_id)
    if not include_ended:
        query = query.where(session_lifetimes.live_clause())
    rows = (await session.exec(query.order_by(UserSession.created_at.desc()).limit(limit))).all()
    now = datetime.now(timezone.utc)
    return [build_session_response(row, session_lifetimes.session_state(row, now)) for row in rows]


@router.post(
    UserRoutes.USER_SESSIONS_END.value,
    response_model=SessionsEndedResponse,
    responses=problems(ErrorCode.USER_NOT_FOUND, AccessErrorCode.TARGET_USER_MORE_PRIVILEGED, *GUARDED),
)
async def end_user_sessions(
        request: Request,
        user_id: Annotated[int, Path(ge=1)],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.USER_VIEW, Permissions.USER_UPDATE]))
):
    """Revoke every live session of a user (revoke_cause ended_by_admin); the account stays as it is and can log
    in again. The caller's own session ends too when user_id is the caller. Archived users: 404.
    Needs user.update covering the user and every assignment of the user grantable by the caller (403
    target_user_more_privileged)."""
    await check_target_user(session, auth, await _get_active_user_or_404(session, user_id), Permissions.USER_UPDATE)
    ended = await AuthService.invalidate_all_user_sessions(
        session=session,
        user_id=user_id,
        cause=SessionRevokeCause.ENDED_BY_ADMIN,
        actor_user_id=auth.user.id,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
    )
    _logger.info(f"User {auth.user.username} ended {ended} sessions of user_id={user_id}")
    return SessionsEndedResponse(ended_count=ended)


@router.post(
    UserRoutes.USER_SESSION_END.value,
    response_model=UserSessionResponse,
    responses=problems(
        ErrorCode.USER_NOT_FOUND, ErrorCode.SESSION_NOT_FOUND, ErrorCode.SESSION_ALREADY_ENDED,
        AccessErrorCode.TARGET_USER_MORE_PRIVILEGED, *GUARDED,
    ),
)
async def end_user_session(
        request: Request,
        user_id: Annotated[int, Path(ge=1)],
        session_id: Annotated[str, Path(max_length=128)],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.USER_VIEW, Permissions.USER_UPDATE]))
):
    """Revoke one live session of a user (revoke_cause ended_by_admin). Ended already: 409 with its end_reason.
    Needs user.update covering the user and every assignment of the user grantable by the caller (403
    target_user_more_privileged)."""
    await check_target_user(session, auth, await _get_active_user_or_404(session, user_id), Permissions.USER_UPDATE)
    row = (await session.exec(
        select(UserSession).where(UserSession.id == session_id, UserSession.user_id == user_id)
    )).first()
    if not row:
        raise ApiError(ErrorCode.SESSION_NOT_FOUND)
    state = session_lifetimes.session_state(row)
    if state.status is SessionStatus.ENDED:
        await session_lifetimes.record_end(session, row, state)
        if state.end_reason:
            raise ApiError(ErrorCode.SESSION_ALREADY_ENDED, end_reason=state.end_reason.value)
        raise ApiError(ErrorCode.SESSION_ALREADY_ENDED)

    session_lifetimes.end(row, SessionEndReason.REVOKED, SessionRevokeCause.ENDED_BY_ADMIN)
    session.add(row)
    await create_audit_log(
        session=session,
        action="force_logout",
        resource_type="session",
        resource_id=user_id,
        user_id=auth.user.id,
        details={"message": f"User {auth.user.username} ended one session of user_id={user_id}",
                 "ended_session_id": row.id, "cause": SessionRevokeCause.ENDED_BY_ADMIN.value},
        **client_info(request, auth)
    )
    await session.commit()
    await session.refresh(row)
    return build_session_response(row, session_lifetimes.session_state(row))
