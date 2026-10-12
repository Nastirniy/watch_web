from typing import List, Optional, Annotated
from fastapi import APIRouter, Depends, status, Form, Request, Response, Query, Path
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError, DataError
from sqlmodel import select, desc
from datetime import datetime, timezone

from app.backend.api.shared.helpers.db_errors import is_data_too_long, is_unique_violation
from app.backend.api.shared.helpers.post_helper import refuse_archived_post
from app.backend.database import get_session
from app.backend.models.user import User, UserSession
from app.backend.models.role import Role, UserRole
from app.backend.models.post import Post
from app.backend.models.user_post_access import UserPostAccess
from app.backend.auth.context import AuthContext
from app.backend.auth.require import require
from app.backend.auth.access_control import AccessControlService
from app.backend.auth.privileges import (
    check_admin_kept, check_an_admin_remains, check_grant, check_target_user, role_permission_names,
)
from app.backend.auth.service import AuthService
from app.backend.auth.certificates import (
    CN_SCHEME_CURRENT, CertificateParser, cn_hash_matches, hash_pki_certificate_data,
)
from app.backend.enums.general import Permissions, SessionEndReason, SessionRevokeCause, SessionStatus
from app.backend.auth import sessions as session_lifetimes
from app.backend.api.v1.models.response.auth import SessionsEndedResponse, UserSessionResponse, build_session_response
from app.backend.enums.routes import UserRoutes
from app.backend.api.shared.helpers.audit_helper import client_info
from app.backend.services.audit import create_audit_log
from app.backend.utils.logging_config import get_api_logger
from app.backend.api.v1.models.request.users import UserCreateForm, UserUpdateForm, UserPostAccessUpsert
from app.backend.api.v1.models.response.users import UserResponse, UserRoleResponse, UserPostAccessResponse
from app.backend.api.v1.models.query.users import UserFilterParams
from app.backend.filters.user_filter import UserFilterService
from app.backend.errors.problems import ApiError
from app.backend.enums.errors import AccessErrorCode, ErrorCode
from app.backend.errors.openapi import ADMIN, GUARDED, problems

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


async def _assignable_roles(session: AsyncSession, role_ids: List[int]) -> List[Role]:
    """The roles to assign; unknown or archived ones are refused before anything is written"""
    roles = []
    for role_id in role_ids:
        role = (await session.exec(select(Role).where(Role.id == role_id))).first()
        if not role:
            raise ApiError(ErrorCode.ROLE_NOT_FOUND, role_id=role_id)
        if role.is_deleted:
            raise ApiError(ErrorCode.ROLE_ARCHIVED_NOT_ASSIGNABLE, role_id=role_id)
        roles.append(role)
    return roles


async def _assigned_role_ids(session: AsyncSession, user_id: int) -> List[int]:
    """Role ids of every assignment row of the user (archived roles included), sorted"""
    return sorted((await session.exec(select(UserRole.role_id).where(UserRole.user_id == user_id))).all())


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
        ErrorCode.ROLE_ARCHIVED_NOT_ASSIGNABLE, ErrorCode.USERNAME_OR_CERTIFICATE_TAKEN, ErrorCode.USERNAME_TAKEN,
        ErrorCode.POST_NOT_FOUND, ErrorCode.POST_ARCHIVED, ErrorCode.ROLE_NOT_FOUND, ErrorCode.CERTIFICATE_FILE_TOO_LARGE,
        ErrorCode.DATA_TOO_LONG, AccessErrorCode.GRANT_EXCEEDS_OWN_PERMISSIONS, *ADMIN,
    ),
)
async def create_user(
        request: Request,
        user_data: Annotated[UserCreateForm, Form()],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.USER_VIEW, Permissions.USER_CREATE], admin=True))
):
    """
    Create new user with PKI certificate authentication

    The certificate file will be parsed to extract:
    - Common Name (CN)
    - Serial Number
    - SHA256 Fingerprint

    These fields are hashed before storage for security.

    The form fields are described by UserCreateForm; role_ids is repeated once per role. The caller must hold
    every permission of the roles (403 grant_exceeds_own_permissions). An archived post cannot be the home post
    (409 post_archived).
    """
    current_user = auth.user
    certificate = user_data.certificate
    username = user_data.username

    # Validate post_id exists if provided
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

    roles_to_assign = await _assignable_roles(session, user_data.role_ids)
    check_grant(auth, await role_permission_names(session, (role.id for role in roles_to_assign)))

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
        pki_certificate_cn_hash=pki_cn_hash,
        pki_certificate_cn_hash_scheme=CN_SCHEME_CURRENT,
        pki_certificate_serial_hash=pki_serial_hash,
        pki_certificate_fingerprint_hash=pki_fingerprint_hash
    )
    session.add(user)

    try:
        # The user and its roles are written by one commit: a refused role row must not leave a user without roles
        await session.flush()
        for role in roles_to_assign:
            session.add(UserRole(user_id=user.id, role_id=role.id))
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
                "role_ids": sorted(role.id for role in roles_to_assign),
            },
            details={"message": f"User {current_user.username} created new user {user.username} with {len(roles_to_assign)} roles"},
            **client_info(request, auth)
        )
        await session.commit()
        
        _logger.info(f"User {current_user.username} created user {user_data.username} with {len(roles_to_assign)} roles and PKI authentication")

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

    result = await session.exec(
        select(User).where(User.id == user_id)
        # select(User).where(User.id == user_id, User.is_deleted is False)  # TODO: implement boolean check
    )
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
        ErrorCode.CERTIFICATE_TAKEN, ErrorCode.ROLE_ARCHIVED_NOT_ASSIGNABLE, ErrorCode.USERNAME_TAKEN,
        ErrorCode.POST_NOT_FOUND, ErrorCode.POST_ARCHIVED, ErrorCode.ROLE_NOT_FOUND, ErrorCode.USER_NOT_FOUND,
        ErrorCode.CERTIFICATE_FILE_TOO_LARGE, ErrorCode.FORM_CONTENT_TYPE_REQUIRED,
        ErrorCode.LAST_ADMIN_REQUIRED, ErrorCode.CANNOT_REMOVE_OWN_ADMIN,
        AccessErrorCode.GRANT_EXCEEDS_OWN_PERMISSIONS, AccessErrorCode.TARGET_USER_MORE_PRIVILEGED,
        *ADMIN,
    ),
)
async def update_user(
        request: Request,
        user_id: Annotated[int, Path(ge=1)],
        update_data: Annotated[UserUpdateForm, Form()],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.USER_VIEW, Permissions.USER_UPDATE], admin=True))
):
    """
    Update user fields - all fields are optional

    Supports updating:
    - username: Unique username (3-50 characters)
    - username_alias: User's display name (1-255 characters)
    - post_id: User's post/position ID; moving the user onto an archived post answers 409 post_archived (sending
      the current home post again changes nothing and is accepted)
    - is_active: User's active status (boolean)
    - role_ids: Replace all user roles, one form field per role (role_ids=1&role_ids=2)
    - certificate: Update PKI certificate authentication data (file upload). A different certificate ends every
      session of the user, like deactivation: the old certificate's tokens stop working.

    Only provided fields will be updated. Archived users answer 404 until restored (POST /{user_id}/restore).
    The fields are form fields: any other body (JSON included) answers 415 instead of being ignored.

    Changing another user needs a user who holds no permission the caller lacks (403
    target_user_more_privileged), new roles only permissions the caller holds (403
    grant_exceeds_own_permissions). A role change or a deactivation must leave an active administrator and the
    caller's own admin.true (409 last_admin_required, cannot_remove_own_admin).
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
    await check_target_user(session, auth, user)

    # Store old values for auditing
    old_values = user.model_dump(exclude={"pki_certificate_cn_hash", "pki_certificate_serial_hash", "pki_certificate_fingerprint_hash"})

    certificate = update_data.certificate
    # Checked before any change: a refused role must not leave the other fields of the PATCH saved
    roles_to_assign = await _assignable_roles(session, update_data.role_ids) if update_data.role_ids is not None else None
    old_role_ids = await _assigned_role_ids(session, user_id)
    new_role_ids = sorted(role.id for role in roles_to_assign) if roles_to_assign is not None else old_role_ids
    roles_change = new_role_ids != old_role_ids
    if roles_change:
        check_grant(auth, await role_permission_names(session, new_role_ids))

    removed_post_access = []
    if update_data.username is not None:
        existing_username = await session.exec(_username_taken(update_data.username, exclude_user_id=user_id))
        if existing_username.first():
            raise ApiError(ErrorCode.USERNAME_TAKEN)
        user.username = update_data.username

    if update_data.post_id is not None:
        post = (await session.exec(select(Post).where(Post.id == update_data.post_id))).first()
        if not post:
            raise ApiError(ErrorCode.POST_NOT_FOUND, post_id=update_data.post_id)
        if post.id != user.post_id:
            refuse_archived_post(post)
        user.post_id = update_data.post_id
        # The home post already grants full access: an extra access row for it would be stale
        for row in (await session.exec(
            select(UserPostAccess).where(UserPostAccess.user_id == user_id, UserPostAccess.post_id == update_data.post_id)
        )).all():
            removed_post_access.append({"post_id": row.post_id, "can_write": row.can_write})
            await session.delete(row)

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
        if roles_change:
            # Replace the role assignments in the same transaction as the other changes
            # By difference: the flush writes INSERTs before DELETEs, so a kept row must not be re-inserted
            wanted = {role.id for role in roles_to_assign}
            present = set()
            for assignment in (await session.exec(select(UserRole).where(UserRole.user_id == user_id))).all():
                if assignment.role_id in wanted:
                    present.add(assignment.role_id)
                else:
                    await session.delete(assignment)
            for role in roles_to_assign:
                if role.id not in present:
                    session.add(UserRole(user_id=user_id, role_id=role.id))
            _logger.info(f"User {current_user.username} replaced the roles of user {user.username} with {len(roles_to_assign)} roles")
        if roles_change:
            await check_admin_kept(session, auth)
        elif deactivating:
            await check_an_admin_remains(session)

        await session.commit()

        await session.refresh(user)
        
        # Create audit log
        await create_audit_log(
            session=session,
            action="update",
            resource_type="user",
            resource_id=user.id,
            user_id=current_user.id,
            old_values={**old_values, "role_ids": old_role_ids},
            new_values={
                **user.model_dump(exclude={"pki_certificate_cn_hash", "pki_certificate_serial_hash", "pki_certificate_fingerprint_hash"}),
                "role_ids": new_role_ids,
                "certificate_replaced": certificate_replaced,
                **({"removed_post_access": removed_post_access} if removed_post_access else {}),
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


# UserRole Management Routes

@router.get(
    UserRoutes.USER_ROLES.value,
    response_model=List[UserRoleResponse],
    responses=problems(ErrorCode.USER_NOT_FOUND, *ADMIN),
)
async def list_user_roles(
        user_id: Annotated[int, Path(ge=1)],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.USER_VIEW, Permissions.ROLE_VIEW], admin=True))
):
    """List all roles assigned to a user - requires both USER_VIEW and ROLE_VIEW permissions"""
    current_user = auth.user

    # Check if user exists
    user_result = await session.exec(
        select(User).where(User.id == user_id, User.is_deleted == False)
    )
    user = user_result.first()

    if not user:
        raise ApiError(ErrorCode.USER_NOT_FOUND)

    # Get all roles for this user
    result = await session.exec(
        select(Role, UserRole.assigned_at)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user_id, Role.is_deleted == False)
        .order_by(desc(UserRole.assigned_at))
    )

    roles_data = result.all()
    _logger.info(f"User {current_user.username} listed {len(roles_data)} roles for user {user.username}")

    return [UserRoleResponse(
        id=role.id,
        name=role.name,
        display_name=role.display_name,
        assigned_at=assigned_at
    ) for role, assigned_at in roles_data]


# =============================================================================
# POST ACCESS (administration): extra posts a user may read or write
# =============================================================================
async def _get_active_user_or_404(session: AsyncSession, user_id: int) -> User:
    result = await session.exec(select(User).where(User.id == user_id, User.is_deleted == False))
    user = result.first()
    if not user:
        raise ApiError(ErrorCode.USER_NOT_FOUND)
    return user


@router.get(
    UserRoutes.USER_POST_ACCESS.value,
    response_model=List[UserPostAccessResponse],
    responses=problems(ErrorCode.USER_NOT_FOUND, *ADMIN),
)
async def list_user_post_access(
        user_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.USER_VIEW, Permissions.POST_VIEW], admin=True))
):
    """Extra post access rows of a user (the home post is users.post_id and is not listed here)"""
    await _get_active_user_or_404(session, user_id)
    result = await session.exec(
        select(UserPostAccess).where(UserPostAccess.user_id == user_id).order_by(UserPostAccess.post_id)
    )
    return [UserPostAccessResponse.model_validate(row) for row in result.all()]


@router.put(
    UserRoutes.USER_POST_ACCESS_BY_POST.value,
    response_model=UserPostAccessResponse,
    responses=problems(
        ErrorCode.POST_ACCESS_HOME_POST, ErrorCode.POST_NOT_FOUND, ErrorCode.POST_ARCHIVED, ErrorCode.USER_NOT_FOUND,
        AccessErrorCode.TARGET_USER_MORE_PRIVILEGED, *ADMIN,
    ),
)
async def upsert_user_post_access(
        request: Request,
        user_id: int,
        post_id: int,
        access_data: UserPostAccessUpsert,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.USER_VIEW, Permissions.USER_UPDATE, Permissions.POST_VIEW], admin=True))
):
    """Grant a user read (can_write=false) or read+write (can_write=true) access to an additional post.
    A user holding permissions the caller lacks: 403 target_user_more_privileged. An archived post: 409 post_archived
    (its existing rows stay and apply again after a restore; DELETE still revokes them)."""
    current_user = auth.user
    user = await _get_active_user_or_404(session, user_id)
    await check_target_user(session, auth, user)

    post = await session.get(Post, post_id)
    if not post:
        raise ApiError(ErrorCode.POST_NOT_FOUND)
    refuse_archived_post(post)

    if user.post_id == post_id:
        raise ApiError(ErrorCode.POST_ACCESS_HOME_POST)

    # A rollback expires every loaded object: keep what is needed afterwards as plain values
    actor_id, actor_name, target_name = current_user.id, current_user.username, user.username

    for attempt in (1, 2):
        result = await session.exec(
            select(UserPostAccess).where(UserPostAccess.user_id == user_id, UserPostAccess.post_id == post_id)
        )
        row = result.first()
        old_values = None
        if row:
            old_values = row.model_dump()
            row.can_write = access_data.can_write
            action = "update"
        else:
            row = UserPostAccess(user_id=user_id, post_id=post_id, can_write=access_data.can_write)
            session.add(row)
            action = "create"

        try:
            await session.commit()
            break
        except IntegrityError as exc:
            # A concurrent request created the row between the check and the insert: the PUT is idempotent,
            # so read the row it made and apply this request on top, as if it had existed
            await session.rollback()
            if attempt == 2 or not is_unique_violation(exc):
                raise
    await session.refresh(row)

    await create_audit_log(
        session=session,
        action=action,
        resource_type="user_post_access",
        resource_id=row.id,
        user_id=actor_id,
        old_values=old_values,
        new_values=row.model_dump(),
        details={"message": f"User {actor_name} granted post {post_id} access to user {target_name} (can_write={row.can_write})"},
        **client_info(request, auth)
    )
    await session.commit()

    _logger.info(f"User {actor_name} set post access user={user_id} post={post_id} can_write={row.can_write}")
    return UserPostAccessResponse.model_validate(row)


@router.delete(
    UserRoutes.USER_POST_ACCESS_BY_POST.value,
    status_code=status.HTTP_204_NO_CONTENT,
    responses=problems(
        ErrorCode.POST_ACCESS_NOT_FOUND, ErrorCode.USER_NOT_FOUND, AccessErrorCode.TARGET_USER_MORE_PRIVILEGED, *ADMIN,
    ),
)
async def delete_user_post_access(
        request: Request,
        user_id: int,
        post_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.USER_VIEW, Permissions.USER_UPDATE, Permissions.POST_VIEW], admin=True))
):
    """Revoke access of a user to an additional post.
    A user holding permissions the caller lacks: 403 target_user_more_privileged."""
    current_user = auth.user
    user = await _get_active_user_or_404(session, user_id)
    await check_target_user(session, auth, user)

    result = await session.exec(
        select(UserPostAccess).where(UserPostAccess.user_id == user_id, UserPostAccess.post_id == post_id)
    )
    row = result.first()
    if not row:
        raise ApiError(ErrorCode.POST_ACCESS_NOT_FOUND)

    old_values = row.model_dump()
    row_id = row.id
    await session.delete(row)
    await session.commit()

    await create_audit_log(
        session=session,
        action="delete",
        resource_type="user_post_access",
        resource_id=row_id,
        user_id=current_user.id,
        old_values=old_values,
        details={"message": f"User {current_user.username} revoked post {post_id} access from user {user.username}"},
        **client_info(request, auth)
    )
    await session.commit()

    _logger.info(f"User {current_user.username} revoked post access user={user_id} post={post_id}")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    UserRoutes.USER_ARCHIVE.value,
    response_model=UserResponse,
    responses=problems(
        ErrorCode.CANNOT_ARCHIVE_SELF, ErrorCode.USER_NOT_FOUND, ErrorCode.LAST_ADMIN_REQUIRED,
        AccessErrorCode.TARGET_USER_MORE_PRIVILEGED, *ADMIN,
    ),
)
async def archive_user(
        request: Request,
        user_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.USER_VIEW, Permissions.USER_ARCHIVE], admin=True))
):
    """Archive a user (is_deleted=true): deactivates the account and invalidates its sessions.

    Nothing is removed: observations, media and role assignments of the user stay. The user is hidden from
    lists unless include_deleted=true. Undo with POST /{user_id}/restore. Already archived or unknown: 404.
    A user holding permissions the caller lacks: 403 target_user_more_privileged; the last administrator: 409.
    """
    current_user = auth.user
    user = await _get_active_user_or_404(session, user_id)
    if user.id == current_user.id:
        raise ApiError(ErrorCode.CANNOT_ARCHIVE_SELF)
    await check_target_user(session, auth, user)

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
        ErrorCode.USER_NOT_FOUND, ErrorCode.USER_NOT_ARCHIVED, AccessErrorCode.TARGET_USER_MORE_PRIVILEGED, *ADMIN,
    ),
)
async def restore_user(
        request: Request,
        user_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.USER_VIEW, Permissions.USER_ARCHIVE], admin=True))
):
    """Restore an archived user (is_deleted=false, is_active=true): the account can log in again. Not archived: 409.
    A user holding permissions the caller lacks: 403 target_user_more_privileged."""
    current_user = auth.user
    result = await session.exec(select(User).where(User.id == user_id))
    user = result.first()
    if not user:
        raise ApiError(ErrorCode.USER_NOT_FOUND)
    if not user.is_deleted:
        raise ApiError(ErrorCode.USER_NOT_ARCHIVED)
    await check_target_user(session, auth, user)

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
    responses=problems(ErrorCode.USER_NOT_FOUND, *ADMIN),
)
async def list_user_sessions(
        user_id: Annotated[int, Path(ge=1)],
        include_ended: bool = Query(False, description="Also list ended sessions, with end_reason and revoke_cause"),
        limit: int = Query(50, ge=1, le=500, description="Newest first"),
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.USER_VIEW], admin=True))
):
    """Login sessions of a user, newest first. status is computed when read: online (a request within
    security.online_threshold_seconds), idle (live, client quiet) or ended; a session past its absolute end or
    idle timeout is ended even if nothing has recorded it yet. Archived users are listed too."""
    if not (await session.exec(select(User.id).where(User.id == user_id))).first():
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
    responses=problems(ErrorCode.USER_NOT_FOUND, AccessErrorCode.TARGET_USER_MORE_PRIVILEGED, *ADMIN),
)
async def end_user_sessions(
        request: Request,
        user_id: Annotated[int, Path(ge=1)],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.USER_VIEW, Permissions.USER_UPDATE], admin=True))
):
    """Revoke every live session of a user (revoke_cause ended_by_admin); the account stays as it is and can log
    in again. The caller's own session ends too when user_id is the caller. Archived users: 404.
    A user holding permissions the caller lacks: 403 target_user_more_privileged."""
    await check_target_user(session, auth, await _get_active_user_or_404(session, user_id))
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
        AccessErrorCode.TARGET_USER_MORE_PRIVILEGED, *ADMIN,
    ),
)
async def end_user_session(
        request: Request,
        user_id: Annotated[int, Path(ge=1)],
        session_id: Annotated[str, Path(max_length=128)],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.USER_VIEW, Permissions.USER_UPDATE], admin=True))
):
    """Revoke one live session of a user (revoke_cause ended_by_admin). Ended already: 409 with its end_reason.
    A user holding permissions the caller lacks: 403 target_user_more_privileged."""
    await check_target_user(session, auth, await _get_active_user_or_404(session, user_id))
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
