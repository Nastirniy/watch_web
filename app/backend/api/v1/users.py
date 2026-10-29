from typing import List, Optional, Annotated, Tuple
from fastapi import APIRouter, Depends, Form, Request, Query, Path, status
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError, DataError
from sqlmodel import select
from datetime import datetime, timezone

from app.backend.api.shared.helpers.db_errors import is_data_too_long, is_unique_violation, unique_violation_as
from app.backend.api.shared.helpers.post_helper import refuse_archived_post
from app.backend.database import get_session
from app.backend.utils.row_locks import lock as lock_row, lock_all, lock_first
from app.backend.models.user import User, UserSession
from app.backend.models.role import Role
from app.backend.models.role_assignment import RoleAssignment, scope_columns
from app.backend.models.system_role_assignment import SystemRoleAssignment
from app.backend.models.post import Post
from app.backend.auth.context import AuthContext
from app.backend.auth.require import require
from app.backend.auth.access_control import AccessControlService
from app.backend.auth.assignments import assignment_views, change_user_type, create_assignment, delete_assignment
from app.backend.auth.system_assignments import (
    create_system_assignment, delete_system_assignment, system_assignment_views,
)
from app.backend.auth.privileges import (
    check_admin_kept, check_an_admin_remains, check_grant_at, check_target_user, lock_administrators,
    role_permission_names,
)
from app.backend.auth.scope import Node, ROOT
from app.backend.auth.user_types import USER_TYPES
from app.backend.auth.service import AuthService
from app.backend.enums.general import (
    CertificateSource, Permissions, SessionEndReason, SessionRevokeCause, SessionStatus, UserType,
)
from app.backend.errors.coded import CodedError
from app.backend.services.certificates import MAX_CERTIFICATE_BYTES, register_certificate
from app.backend.auth import sessions as session_lifetimes
from app.backend.api.v1.models.response.auth import SessionsEndedResponse, UserSessionResponse, build_session_response
from app.backend.enums.routes import UserRoutes
from app.backend.api.shared.helpers.audit_helper import client_info
from app.backend.enums.audit import AuditEvent
from app.backend.services.audit import create_audit_log, home_scope, node_name, role_name
from app.backend.services.units import membership_nodes
from app.backend.utils.logging_config import get_api_logger
from app.backend.api.v1.models.request.assignments import AssignmentCreate, SystemAssignmentCreate
from app.backend.api.v1.models.request.users import UserCreateForm, UserUpdateForm
from app.backend.api.v1.models.response.assignments import AssignmentResponse, SystemAssignmentResponse
from app.backend.api.v1.models.response.users import UserResponse, user_to_response, users_to_response
from app.backend.api.v1.models.query.users import UserFilterParams
from app.backend.filters.user_filter import UserFilterService
from app.backend.errors.problems import ApiError
from app.backend.enums.errors import AccessErrorCode, ErrorCode
from app.backend.errors.openapi import GUARDED, problems

router = APIRouter(prefix=UserRoutes.BASE.value, tags=[UserRoutes.tag()])
_logger = get_api_logger()


def taken_error_code(exc: IntegrityError) -> ErrorCode | None:
    """The problem for a unique violation on users.username, user_certificates.fingerprint_hash or the unique index of
    role_assignments (a type change met a concurrent assignment of the new base role at the same node), otherwise None.

    e.orig holds only the database message (sqlite: "users.username", MySQL: the key name, which contains the column);
    str(exc) also carries the SQL, which names every column, so it is never searched.
    """
    if not is_unique_violation(exc):
        return None
    message = str(exc.orig)
    if "username" in message:
        return ErrorCode.USERNAME_TAKEN
    if "fingerprint_hash" in message:
        return ErrorCode.CERTIFICATE_TAKEN
    if "uq_role_assignments_user_role_scope" in message or "role_assignments.scope_key" in message:
        return ErrorCode.ASSIGNMENT_EXISTS
    return None

# PATCH /users/{id} reads form fields only; a JSON body would reach the route with every field unset
FORM_CONTENT_TYPES = ("multipart/form-data", "application/x-www-form-urlencoded")


async def _base_assignments(
        session: AsyncSession, auth: AuthContext, user_type: UserType, post_id: Optional[int],
) -> List[Tuple[Role, Optional[Node]]]:
    """The first assignments of a new user, the base roles of the type (auth/user_types.py): the tree base role at
    the home post (at the root without one or for an at_root base role), each system base role without a node
    (None). The caller needs user.create covering the node of the tree base role (403
    permission_required_for_action) and every permission of each base role where it acts, the root for a system
    role (403 grant_exceeds_own_permissions)."""
    spec = USER_TYPES[user_type]
    tree = spec.tree_base
    node = ROOT if post_id is None or tree.at_root else Node.post(post_id)
    AccessControlService.check_covers(auth, Permissions.USER_CREATE, node, "create_user")
    base: List[Tuple[Role, Optional[Node]]] = []
    for item in (tree, *spec.system_bases):
        role = (await session.exec(select(Role).where(Role.builtin_key == item.role.value))).one()
        check_grant_at(auth, await role_permission_names(session, [role.id]), node if item is tree else ROOT)
        base.append((role, node if item is tree else None))
    return base


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

    post_id, brigade_id and corps_id match the home node or an assignment at the node or below, chosen by
    membership (any, home, assigned).
    """
    current_user = auth.user
    archived = AccessControlService.archived_scope(auth, filters, Permissions.USER_ARCHIVE)
    AccessControlService.resolve_scope(auth, Permissions.USER_VIEW, filters.post_id)   # 403 for a post the caller cannot see
    scope = AccessControlService.resolve_scope(auth, Permissions.USER_VIEW, None)      # the post_id filter is a membership

    query = select(User)
    query = UserFilterService.apply_filters(query, filters, scope, archived_post_ids=archived, caller_id=current_user.id,
                                            unit_nodes=membership_nodes(auth.tree, filters),
                                            assigned_nodes=membership_nodes(auth.tree, filters, live_only=True))
    result = await session.exec(query)
    users = result.all()

    _logger.info(f"User {current_user.username} listed {len(users)} users (scope={sorted(scope) if scope is not None else 'all'})")

    return await users_to_response(session, auth.tree, users)


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
    Create a new user, optionally with a certificate file the user logs in with

    With certificate (PEM or DER) the user gets a certificate row (source uploaded) with its plain values (CN,
    serial, validity), the hashes the login matches and certificate_label as its readable name (default: the CN cut to
    64 characters). The same certificate registered already, for any user, answers 400 certificate_taken; an expired
    one is accepted. Without a certificate the user exists but cannot log in until one is issued or uploaded.

    The form fields are described by UserCreateForm. The user gets the base roles of user_type (GET /user-types): the
    tree base role at the home post (post_id), or at the root without a home post or for a type whose base role
    acts at the root (system_administrator), and the system base roles. The caller needs user.create covering that
    node (403 permission_required_for_action) and every permission of each base role where it acts (403
    grant_exceeds_own_permissions). These checks answer first: a post_id
    outside the caller's scope gets the same 403 whether the post exists, is archived or not. An unknown post answers
    404 post_not_found and an archived post 409 post_archived (it cannot be the home post).
    """
    current_user = auth.user
    certificate = user_data.certificate

    # Scope before the post: an outsider gets the same 403 for an unknown, an archived and a live post outside it
    base_roles = await _base_assignments(session, auth, user_data.user_type, user_data.post_id)
    if user_data.post_id is not None:
        post = (await session.exec(select(Post).where(Post.id == user_data.post_id))).first()
        if not post:
            raise ApiError(ErrorCode.POST_NOT_FOUND, post_id=user_data.post_id)
        refuse_archived_post(post)

    # Check if user already exists
    result = await session.exec(_username_taken(user_data.username))
    if result.first():
        raise ApiError(ErrorCode.USERNAME_TAKEN)

    # The certificate file before anything is written; it is parsed when its row is registered
    cert_data = None
    if certificate is not None:
        cert_data = await certificate.read()
        if len(cert_data) == 0:
            raise ApiError(ErrorCode.CERTIFICATE_FILE_EMPTY)
        if len(cert_data) > MAX_CERTIFICATE_BYTES:
            raise ApiError(ErrorCode.CERTIFICATE_FILE_TOO_LARGE, max_bytes=MAX_CERTIFICATE_BYTES)

    user = User(
        username=user_data.username,
        username_alias=user_data.username_alias,
        post_id=user_data.post_id,
        user_type=user_data.user_type.value,
    )
    session.add(user)

    try:
        # The user, its assignment and its certificate are written by one commit: a user never exists without an
        # assignment, a refused certificate leaves no user
        await session.flush()
        for base_role, node in base_roles:
            if node is None:
                session.add(SystemRoleAssignment(user_id=user.id, role_id=base_role.id, created_by=current_user.id))
            else:
                session.add(RoleAssignment(user_id=user.id, role_id=base_role.id, created_by=current_user.id,
                                           **scope_columns(node)))
        certificate_row = None
        if cert_data is not None:
            try:
                certificate_row = await register_certificate(
                    session, user, cert_data, user_data.certificate_label, CertificateSource.UPLOADED, current_user.id)
            except CodedError as exc:
                await session.rollback()
                raise ApiError.from_coded(exc)
        certificate_id = certificate_row.id if certificate_row is not None else None
        await session.refresh(user)

        # The audit row joins the same transaction: one commit
        await create_audit_log(
            session,
            AuditEvent.USER_CREATE,
            params={"username": user.username, "user_type": user.user_type},
            scope=home_scope(user),
            resource_id=user.id,
            user_id=current_user.id,
            new_values={
                **user.model_dump(),
                "assignments": [{"role_id": role.id, "scope": node.key} for role, node in base_roles if node],
                "system_assignments": [{"role_id": role.id} for role, node in base_roles if node is None],
                "certificate_id": certificate_id,
            },
            **client_info(request, auth)
        )
        await session.commit()
        
        _logger.info(f"User {current_user.username} created user {user_data.username} ({user.user_type}), "
                     f"certificate_id={certificate_id}")

        return await user_to_response(session, auth.tree, user)
    except (IntegrityError, DataError) as e:
        await session.rollback()
        if is_data_too_long(e):
            raise ApiError(ErrorCode.DATA_TOO_LONG)
        if isinstance(e, IntegrityError) and is_unique_violation(e):
            raise ApiError(ErrorCode.USERNAME_OR_CERTIFICATE_TAKEN)
        raise


async def _lock_role_then_user(session: AsyncSession, user_id: int, role_id: int, administrators: bool) -> User:
    """The locks of an assignment write, in one order, before any plain read: the administrators' lock (system
    assignments), the role row (shared), then the user. Locking the User entity reads the presence subqueries
    (models/presence_properties.py), a plain read that takes the snapshot on MariaDB: the role is locked before it,
    so a concurrent edit of the role's permissions is seen whole, before or after (R40), never a 1020."""
    if administrators:
        await lock_administrators(session)
        await lock_row(session, select(Role.id).where(Role.id == role_id), shared=True)
    else:
        await lock_first(session, select(Role.id).where(Role.id == role_id), shared=True)
    user = await lock_row(session, select(User).where(User.id == user_id))
    if not user:
        raise ApiError(ErrorCode.USER_NOT_FOUND)
    return user


async def _lock_user_and_roles(session: AsyncSession, user_id: int) -> User:
    """The locks of a type change, in one order, before any plain read: the administrators' lock, the user row (its id
    only), the user's assignments and their roles (shared), then the user entity. Locking the User entity reads the
    presence subqueries, a plain read that takes the snapshot on MariaDB: a permission added to one of the roles
    meanwhile (the role edit locks the role row first and reads the types of its holders) is seen whole, before or
    after, and one of the two answers 409, never both succeed. The assignments of the user stay as read: every
    writer of them locks the user first. An archived user takes no role locks (the route answers 404)."""
    await lock_administrators(session)
    found = await lock_row(session, select(User.id, User.is_deleted).where(User.id == user_id))
    if found is not None and not found[1]:
        role_ids = {
            *await lock_all(session, select(RoleAssignment.role_id).where(RoleAssignment.user_id == user_id), shared=True),
            *await lock_all(session, select(SystemRoleAssignment.role_id).where(SystemRoleAssignment.user_id == user_id),
                            shared=True),
        }
        if role_ids:
            await lock_all(session, select(Role.id).where(Role.id.in_(sorted(role_ids))).order_by(Role.id), shared=True)
    user = await lock_row(session, select(User).where(User.id == user_id))
    if not user:
        raise ApiError(ErrorCode.USER_NOT_FOUND)
    return user


async def _get_user_or_404(session: AsyncSession, user_id: int, lock: Optional[str] = None) -> User:
    """The user, archived or not (each route decides what an archived user means after its scope check).

    lock (utils/row_locks.py) for writes: "user" locks the row as the first statement of the write, so changes of one
    user, its assignments included, run one after another and each sees the one before (a user keeps at least one
    assignment); "administrators" first locks the built-in admin role (lock_administrators) for writes that may take
    it from someone, then the user."""
    query = select(User).where(User.id == user_id)
    if lock == "administrators":
        await lock_administrators(session)
        user = await lock_row(session, query)
    elif lock == "user":
        user = await lock_first(session, query)
    else:
        user = (await session.exec(query)).first()
    if not user:
        raise ApiError(ErrorCode.USER_NOT_FOUND)
    return user


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
    user = await _get_user_or_404(session, user_id)

    # Visibility before the state: an outsider gets the same 403 for an archived and a live user
    AccessControlService.check_user_view(auth, user)

    # Archived users are for those who manage the archive where the user belongs
    if user.is_deleted and not auth.covers_user(Permissions.USER_ARCHIVE, user):
        raise ApiError(ErrorCode.USER_NOT_FOUND)

    return await user_to_response(session, auth.tree, user)


@router.patch(
    UserRoutes.USER_BY_ID.value,
    response_model=UserResponse,
    responses=problems(
        ErrorCode.CANNOT_DEACTIVATE_SELF, ErrorCode.USERNAME_TAKEN, ErrorCode.POST_NOT_FOUND, ErrorCode.POST_ARCHIVED,
        ErrorCode.USER_NOT_FOUND, ErrorCode.FORM_CONTENT_TYPE_REQUIRED, ErrorCode.LAST_ADMIN_REQUIRED,
        ErrorCode.CANNOT_REMOVE_OWN_ADMIN, ErrorCode.USER_TYPE_CEILING_EXCEEDED, ErrorCode.ASSIGNMENT_EXISTS,
        AccessErrorCode.USER_SELF_ONLY, AccessErrorCode.TARGET_USER_MORE_PRIVILEGED,
        AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION, AccessErrorCode.GRANT_EXCEEDS_OWN_PERMISSIONS, *GUARDED,
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
    - user_type: Another type (GET /user-types). Every assignment of the old type's built-in role gets the new type's
      built-in role at the same node (at the root when the new one holds system permissions, as for a new user; an
      assignment of the new role the user already has at that node is kept and the old one merged into it). The
      caller needs user.update and every permission of the new built-in role at each of these nodes and at the node
      a new user of the type would get it (the home post; the root without one or for a system role), also when
      nothing is replaced (403 grant_exceeds_own_permissions); every other assignment, those of archived roles
      included, must fit the ceiling of the new type (409 user_type_ceiling_exceeded with the assignments and their
      excess_permissions). A concurrent assignment of the new built-in role at the same node answers 409
      assignment_exists. The user's sessions end (revoke_cause user_type_changed), so the web never shows the pages
      of the old type. The same type again changes nothing.

    Only provided fields will be updated. Archived users answer 404 until restored (POST /{user_id}/restore).
    Certificates are not changed here: a new one is issued or uploaded and the old one revoked (a certificate form
    field is ignored).
    The fields are form fields: any other body (JSON included) answers 415 instead of being ignored.

    Changing a user needs the user visible to the caller (user.view covering them, as GET /users/{user_id}: 403
    user_self_only), user.update covering them (their home post, the root for a user without one) and, for
    another user, that the caller could grant every assignment they have, each with all of its role's permissions
    whatever the user's type puts in effect (403 target_user_more_privileged); this
    answers before the archived state of the user. A deactivation or a type change must leave an active holder of the
    built-in admin role at the root (409 last_admin_required), and a caller cannot take it from themselves (409
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

    if update_data.user_type is not None:
        user = await _lock_user_and_roles(session, user_id)
    else:
        user = await _get_user_or_404(session, user_id, lock="administrators")
    # Scope before the state: an outsider gets the same 403 for an archived and a live user
    await check_target_user(session, auth, user, Permissions.USER_UPDATE)
    if user.is_deleted:
        raise ApiError(ErrorCode.USER_NOT_FOUND)

    # Store old values for auditing
    old_values = user.model_dump()

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

    # Update timestamp
    user.updated_at = datetime.now(timezone.utc)
    session.add(user)

    # Everything below writes (the type change's reads flush the fields changed above): a unique index that refuses
    # a row a concurrent request took after the checks above answers its 409 / 400 instead of a 500, and the audit
    # row is written in the same transaction as the change
    try:
        # Spec section 6 rule 4 (auth/assignments.py): None when the type stays the same
        type_change = None
        if update_data.user_type is not None:
            type_change = await change_user_type(session, auth, user, update_data.user_type)

        # Deactivation and a new type end every live session in the same transaction as the change, so the user's
        # tokens stop working the moment the update is committed (the request dependency rejects them). A new type
        # changes the pages of the web client: a login of the old type must not keep showing them.
        revoke_cause = (SessionRevokeCause.ACCOUNT_DEACTIVATED if deactivating
                        else SessionRevokeCause.USER_TYPE_CHANGED if type_change is not None else None)
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

        if type_change is not None:
            # The old type's built-in role may have been the admin role at the root: the caller keeps theirs, and
            # someone keeps it (a deactivation too)
            await check_admin_kept(session, auth, type_change.admin_held_before)
        elif deactivating:
            # A change that may take the admin role at the root from its last holder (deactivating oneself is
            # refused above)
            await check_an_admin_remains(session)

        new_values = user.model_dump()
        if type_change is not None:
            new_values["replaced_assignments"] = type_change.replaced
            new_values["removed_system_assignments"] = type_change.system_removed
            new_values["added_system_assignments"] = type_change.system_added
        await create_audit_log(
            session,
            AuditEvent.USER_UPDATE,
            params={"username": user.username},
            scope=home_scope(user),
            resource_id=user.id,
            user_id=current_user.id,
            old_values=old_values,
            new_values=new_values,
            **client_info(request, auth)
        )
        await session.commit()
        await session.refresh(user)

        _logger.info(f"User {current_user.username} updated user {user.username}")

        return await user_to_response(session, auth.tree, user)

    except IntegrityError as e:
        await session.rollback()
        # A concurrent request took the username or the new base role at the node after the checks
        code = taken_error_code(e)
        if code is None:
            raise
        raise ApiError(code)


@router.post(
    UserRoutes.USER_ARCHIVE.value,
    response_model=UserResponse,
    responses=problems(
        ErrorCode.CANNOT_ARCHIVE_SELF, ErrorCode.USER_NOT_FOUND, ErrorCode.LAST_ADMIN_REQUIRED,
        AccessErrorCode.USER_SELF_ONLY, AccessErrorCode.TARGET_USER_MORE_PRIVILEGED, *GUARDED,
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
    Needs user.view covering the user (403 user_self_only, as GET /users/{user_id}), user.archive covering the user
    and every assignment of the user grantable by the caller (403 target_user_more_privileged), answered before the archived state of the user, so an outsider gets the same 403
    for an archived and a live user; the last administrator: 409.
    """
    current_user = auth.user
    user = await _get_user_or_404(session, user_id, lock="administrators")
    if user.id == current_user.id:
        raise ApiError(ErrorCode.CANNOT_ARCHIVE_SELF)
    # Scope before the state: an outsider gets the same 403 for an archived and a live user
    await check_target_user(session, auth, user, Permissions.USER_ARCHIVE)
    if user.is_deleted:
        raise ApiError(ErrorCode.USER_NOT_FOUND)

    old_values = user.model_dump()
    user.is_deleted = True
    user.is_active = False
    user.updated_at = datetime.now(timezone.utc)
    session.add(user)
    await check_an_admin_remains(session)

    await AuthService.invalidate_all_user_sessions(
        session=session,
        user_id=user_id,
        cause=SessionRevokeCause.ACCOUNT_ARCHIVED,
        actor_user_id=current_user.id,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        commit=False,
    )
    await create_audit_log(
        session,
        AuditEvent.USER_ARCHIVE,
        params={"username": user.username},
        scope=home_scope(user),
        resource_id=user.id,
        user_id=current_user.id,
        old_values=old_values,
        **client_info(request, auth)
    )
    await session.commit()
    await session.refresh(user)
    _logger.info(f"User {current_user.username} archived user {user.username}")
    return await user_to_response(session, auth.tree, user)


@router.post(
    UserRoutes.USER_RESTORE.value,
    response_model=UserResponse,
    responses=problems(
        ErrorCode.USER_NOT_FOUND, ErrorCode.USER_NOT_ARCHIVED, AccessErrorCode.USER_SELF_ONLY,
        AccessErrorCode.TARGET_USER_MORE_PRIVILEGED, *GUARDED,
    ),
)
async def restore_user(
        request: Request,
        user_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.USER_VIEW, Permissions.USER_ARCHIVE]))
):
    """Restore an archived user (is_deleted=false, is_active=true): the account can log in again. Not archived: 409.
    Needs user.view covering the user (403 user_self_only, as GET /users/{user_id}), user.archive covering the user
    and every assignment of the user grantable by the caller (403 target_user_more_privileged), answered before the archived state of the user."""
    current_user = auth.user
    user = await _get_user_or_404(session, user_id, lock="user")
    # Scope before the state: an outsider gets the same 403 for an archived and a live user
    await check_target_user(session, auth, user, Permissions.USER_ARCHIVE)
    if not user.is_deleted:
        raise ApiError(ErrorCode.USER_NOT_ARCHIVED)

    old_values = user.model_dump()
    user.is_deleted = False
    user.is_active = True
    user.updated_at = datetime.now(timezone.utc)
    session.add(user)
    await create_audit_log(
        session,
        AuditEvent.USER_RESTORE,
        params={"username": user.username},
        scope=home_scope(user),
        resource_id=user.id,
        user_id=current_user.id,
        old_values=old_values,
        **client_info(request, auth)
    )
    await session.commit()
    await session.refresh(user)
    _logger.info(f"User {current_user.username} restored user {user.username}")
    return await user_to_response(session, auth.tree, user)


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
    target = await _get_user_or_404(session, user_id)
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
    responses=problems(
        ErrorCode.USER_NOT_FOUND, AccessErrorCode.USER_SELF_ONLY, AccessErrorCode.TARGET_USER_MORE_PRIVILEGED, *GUARDED,
    ),
)
async def end_user_sessions(
        request: Request,
        user_id: Annotated[int, Path(ge=1)],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.USER_VIEW, Permissions.USER_UPDATE]))
):
    """Revoke every live session of a user (revoke_cause ended_by_admin); the account stays as it is and can log
    in again. The caller's own session ends too when user_id is the caller. Archived users: 404.
    Needs user.view covering the user (403 user_self_only, as GET /users/{user_id}), user.update covering the user
    and every assignment of the user grantable by the caller (403 target_user_more_privileged), answered before the
    archived state of the user."""
    target = await _get_user_or_404(session, user_id, lock="user")
    # Scope before the state: an outsider gets the same 403 for an archived and a live user
    await check_target_user(session, auth, target, Permissions.USER_UPDATE)
    if target.is_deleted:
        raise ApiError(ErrorCode.USER_NOT_FOUND)
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
        AccessErrorCode.USER_SELF_ONLY, AccessErrorCode.TARGET_USER_MORE_PRIVILEGED, *GUARDED,
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
    Archived users: 404. Needs user.view covering the user (403 user_self_only, as GET /users/{user_id}), user.update
    covering the user and every assignment of the user grantable by the caller (403 target_user_more_privileged),
    answered before the archived state of the user and the session."""
    target = await _get_user_or_404(session, user_id, lock="user")
    # Scope before the state: an outsider gets the same 403 for an archived and a live user
    await check_target_user(session, auth, target, Permissions.USER_UPDATE)
    if target.is_deleted:
        raise ApiError(ErrorCode.USER_NOT_FOUND)
    row = (await session.exec(
        select(UserSession).where(UserSession.id == session_id, UserSession.user_id == user_id)
    )).first()
    if not row:
        raise ApiError(ErrorCode.SESSION_NOT_FOUND)
    state = session_lifetimes.session_state(row)
    if state.status is SessionStatus.ENDED:
        await session_lifetimes.record_end(session, row, state, begin=False)   # the user is locked: a write began
        if state.end_reason:
            raise ApiError(ErrorCode.SESSION_ALREADY_ENDED, end_reason=state.end_reason.value)
        raise ApiError(ErrorCode.SESSION_ALREADY_ENDED)

    session_lifetimes.end(row, SessionEndReason.REVOKED, SessionRevokeCause.ENDED_BY_ADMIN)
    session.add(row)
    await create_audit_log(
        session,
        AuditEvent.SESSION_END,
        params={"username": target.username, "cause": SessionRevokeCause.ENDED_BY_ADMIN.value,
                "ended_session_id": row.id},
        scope=home_scope(target),
        resource_id=user_id,
        user_id=auth.user.id,
        **client_info(request, auth)
    )
    await session.commit()
    await session.refresh(row)
    return build_session_response(row, session_lifetimes.session_state(row))


# Role assignments: a role held at one node of the scope tree (auth/assignments.py has the rules and their order)

@router.get(
    UserRoutes.USER_ASSIGNMENTS.value,
    response_model=List[AssignmentResponse],
    responses=problems(ErrorCode.USER_NOT_FOUND, AccessErrorCode.USER_SELF_ONLY, *GUARDED),
)
async def list_user_assignments(
        user_id: Annotated[int, Path(ge=1)],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.USER_VIEW]))
):
    """The role assignments of a user, by id: the role and the node it applies to (root, group or post, with its
    name; the application name at the root), assignments of archived roles included (role.is_deleted: they grant
    nothing until the role is restored). user.view must cover the user (403 user_self_only otherwise); an archived user, like
    GET /{user_id}, only for callers with user.archive covering them (404 otherwise)."""
    target = await _get_user_or_404(session, user_id)
    # Visibility before the state: an outsider gets the same 403 for an archived and a live user
    AccessControlService.check_user_view(auth, target)
    if target.is_deleted and not auth.covers_user(Permissions.USER_ARCHIVE, target):
        raise ApiError(ErrorCode.USER_NOT_FOUND)
    return [AssignmentResponse.model_validate(view, from_attributes=True)
            for view in await assignment_views(session, target.id)]


@router.post(
    UserRoutes.USER_ASSIGNMENTS.value,
    response_model=AssignmentResponse,
    status_code=status.HTTP_201_CREATED,
    responses=problems(
        ErrorCode.USER_NOT_FOUND, ErrorCode.ROLE_NOT_FOUND, ErrorCode.SCOPE_GROUP_NOT_FOUND, ErrorCode.POST_NOT_FOUND,
        ErrorCode.ROLE_IS_SYSTEM, ErrorCode.ROLE_EXCEEDS_USER_TYPE, ErrorCode.ASSIGNMENT_EXISTS,
        AccessErrorCode.USER_SELF_ONLY, AccessErrorCode.TARGET_USER_MORE_PRIVILEGED,
        AccessErrorCode.GRANT_EXCEEDS_OWN_PERMISSIONS, *GUARDED,
    ),
)
async def create_user_assignment(
        request: Request,
        user_id: Annotated[int, Path(ge=1)],
        data: AssignmentCreate,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.USER_VIEW, Permissions.USER_UPDATE]))
):
    """Give the user a tree role at a node of the scope tree: scope {kind: root} or {kind: group | post, id}. The
    role applies to the node and everything below it, posts added later included. A system role is given through
    POST /{user_id}/system-assignments (409 role_is_system here).

    Checked in this order, so that a node outside the caller's scope answers the same 403 whether it exists, is
    archived or not: user.view covering the user (403 user_self_only, as GET /users/{user_id}); user.update covering
    the user and every assignment of the user grantable by the caller (403 target_user_more_privileged); an archived user 404 user_not_found; an unknown or archived role 404
    role_not_found; a system role 409 role_is_system; user.update and every permission of the role held by the
    caller at the node (403 grant_exceeds_own_permissions, missing lists both; a role without permissions still needs
    user.update there); an unknown or archived group or post 404 scope_group_not_found / post_not_found; a role above
    the tree boundary of the user's type 409 role_exceeds_user_type; the same role at the same node 409
    assignment_exists.
    """
    target = await _lock_role_then_user(session, user_id, data.role_id, administrators=False)
    assignment = await create_assignment(session, auth, target, data.role_id, data.scope.node)
    async with unique_violation_as(session, ErrorCode.ASSIGNMENT_EXISTS):
        await session.flush()
    await create_audit_log(
        session,
        AuditEvent.ROLE_ASSIGNMENT_CREATE,
        params={"username": target.username, "role_name": await role_name(session, assignment.role_id),
                "node_name": await node_name(session, assignment.node)},
        scope=assignment.node,
        resource_id=assignment.id,
        user_id=auth.user.id,
        new_values=assignment.model_dump(),
        **client_info(request, auth)
    )
    await session.commit()
    (view,) = await assignment_views(session, target.id, assignment_id=assignment.id)
    return AssignmentResponse.model_validate(view, from_attributes=True)


@router.delete(
    UserRoutes.USER_ASSIGNMENT_BY_ID.value,
    status_code=status.HTTP_204_NO_CONTENT,
    responses=problems(
        ErrorCode.USER_NOT_FOUND, ErrorCode.ASSIGNMENT_NOT_FOUND, ErrorCode.LAST_ASSIGNMENT_REQUIRED,
        ErrorCode.LAST_ADMIN_REQUIRED, ErrorCode.CANNOT_REMOVE_OWN_ADMIN, AccessErrorCode.USER_SELF_ONLY,
        AccessErrorCode.TARGET_USER_MORE_PRIVILEGED, AccessErrorCode.GRANT_EXCEEDS_OWN_PERMISSIONS, *GUARDED,
    ),
)
async def delete_user_assignment(
        request: Request,
        user_id: Annotated[int, Path(ge=1)],
        assignment_id: Annotated[int, Path(ge=1)],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.USER_VIEW, Permissions.USER_UPDATE]))
):
    """Remove one role assignment of the user (the link row; the role stays). The audit log keeps a full copy of the
    removed row.

    Checked in this order: user.view covering the user (403 user_self_only, as GET /users/{user_id}); user.update
    covering the user and every assignment of the user grantable by the caller (403 target_user_more_privileged); an archived user 404 user_not_found; an assignment that is not this user's
    404 assignment_not_found; user.update and every permission of its role held by the caller at its node (403
    grant_exceeds_own_permissions); the user's last assignment of either kind 409 last_assignment_required (an
    assignment of a live role needs another one of a live role to remain: an assignment of an archived role grants
    nothing; one of an archived role needs any other assignment to remain); the built-in administrator role at the
    root is half of the administrator: an administrator (system_administrator and administrator at the root, active)
    must remain (409 last_admin_required) and it cannot be taken from oneself while one (409 cannot_remove_own_admin).
    """
    target = await _get_user_or_404(session, user_id, lock="administrators")
    removed = await delete_assignment(session, auth, target, assignment_id)
    removed_node = Node.parse(removed["scope_key"])
    await create_audit_log(
        session,
        AuditEvent.ROLE_ASSIGNMENT_DELETE,
        params={"username": target.username, "role_name": await role_name(session, removed["role_id"]),
                "node_name": await node_name(session, removed_node)},
        scope=removed_node,
        resource_id=assignment_id,
        user_id=auth.user.id,
        old_values=removed,
        **client_info(request, auth)
    )
    await session.commit()


# System role assignments: a system role, no node (auth/system_assignments.py has the rules and their order)

@router.get(
    UserRoutes.USER_SYSTEM_ASSIGNMENTS.value,
    response_model=List[SystemAssignmentResponse],
    responses=problems(ErrorCode.USER_NOT_FOUND, AccessErrorCode.USER_SELF_ONLY, *GUARDED),
)
async def list_user_system_assignments(
        user_id: Annotated[int, Path(ge=1)],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.USER_VIEW]))
):
    """The system role assignments of a user, by id, those of archived roles included (role.is_deleted: they grant
    nothing until the role is restored). Visibility as GET /{user_id}/assignments: user.view must cover the user (403
    user_self_only otherwise); an archived user only for callers with user.archive covering them (404 otherwise)."""
    target = await _get_user_or_404(session, user_id)
    AccessControlService.check_user_view(auth, target)
    if target.is_deleted and not auth.covers_user(Permissions.USER_ARCHIVE, target):
        raise ApiError(ErrorCode.USER_NOT_FOUND)
    return [SystemAssignmentResponse.model_validate(view, from_attributes=True)
            for view in await system_assignment_views(session, target.id)]


@router.post(
    UserRoutes.USER_SYSTEM_ASSIGNMENTS.value,
    response_model=SystemAssignmentResponse,
    status_code=status.HTTP_201_CREATED,
    responses=problems(
        ErrorCode.USER_NOT_FOUND, ErrorCode.ROLE_NOT_FOUND, ErrorCode.ROLE_IS_TREE, ErrorCode.ROLE_EXCEEDS_USER_TYPE,
        ErrorCode.ASSIGNMENT_EXISTS, AccessErrorCode.USER_SELF_ONLY, AccessErrorCode.TARGET_USER_MORE_PRIVILEGED,
        AccessErrorCode.GRANT_EXCEEDS_OWN_PERMISSIONS, *GUARDED,
    ),
)
async def create_user_system_assignment(
        request: Request,
        user_id: Annotated[int, Path(ge=1)],
        data: SystemAssignmentCreate,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.USER_VIEW, Permissions.USER_UPDATE]))
):
    """Give the user a system role: it acts on the whole system, without a node.

    Checked in this order: user.view covering the user (403 user_self_only, as GET /users/{user_id}); user.update
    covering the user and every assignment of the user grantable by the caller (403 target_user_more_privileged); an archived user 404 user_not_found; an unknown or archived role 404
    role_not_found; a tree role 409 role_is_tree (POST /{user_id}/assignments gives those); user.update and every
    permission of the role held by the caller at the root (403 grant_exceeds_own_permissions); a role above the
    system boundary of the user's type 409 role_exceeds_user_type; the same role 409 assignment_exists.
    """
    # The administrators' lock first, as every route that may touch the system_administrator role (one lock order)
    target = await _lock_role_then_user(session, user_id, data.role_id, administrators=True)
    assignment = await create_system_assignment(session, auth, target, data.role_id)
    async with unique_violation_as(session, ErrorCode.ASSIGNMENT_EXISTS):
        await session.flush()
    await create_audit_log(
        session,
        AuditEvent.SYSTEM_ROLE_ASSIGNMENT_CREATE,
        params={"username": target.username, "role_name": await role_name(session, assignment.role_id)},
        resource_id=assignment.id,
        user_id=auth.user.id,
        new_values=assignment.model_dump(),
        **client_info(request, auth)
    )
    await session.commit()
    view = next(view for view in await system_assignment_views(session, target.id) if view.id == assignment.id)
    return SystemAssignmentResponse.model_validate(view, from_attributes=True)


@router.delete(
    UserRoutes.USER_SYSTEM_ASSIGNMENT_BY_ID.value,
    status_code=status.HTTP_204_NO_CONTENT,
    responses=problems(
        ErrorCode.USER_NOT_FOUND, ErrorCode.ASSIGNMENT_NOT_FOUND, ErrorCode.LAST_ASSIGNMENT_REQUIRED,
        ErrorCode.LAST_ADMIN_REQUIRED, ErrorCode.CANNOT_REMOVE_OWN_ADMIN, AccessErrorCode.USER_SELF_ONLY,
        AccessErrorCode.TARGET_USER_MORE_PRIVILEGED, AccessErrorCode.GRANT_EXCEEDS_OWN_PERMISSIONS, *GUARDED,
    ),
)
async def delete_user_system_assignment(
        request: Request,
        user_id: Annotated[int, Path(ge=1)],
        assignment_id: Annotated[int, Path(ge=1)],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.USER_VIEW, Permissions.USER_UPDATE]))
):
    """Remove one system role assignment of the user (the role stays). The audit log keeps a full copy of the
    removed row.

    Checked in this order: user.view covering the user (403 user_self_only, as GET /users/{user_id}); user.update
    covering the user and every assignment of the user grantable by the caller (403 target_user_more_privileged); an archived user 404 user_not_found; a system assignment that is not this
    user's 404 assignment_not_found; user.update and every permission of its role held by the caller at the root
    (403 grant_exceeds_own_permissions); the user's last assignment of either kind 409 last_assignment_required; the
    built-in system_administrator role must stay with an active user (409 last_admin_required) and cannot be taken
    from oneself (409 cannot_remove_own_admin).
    """
    target = await _get_user_or_404(session, user_id, lock="administrators")
    removed = await delete_system_assignment(session, auth, target, assignment_id)
    await create_audit_log(
        session,
        AuditEvent.SYSTEM_ROLE_ASSIGNMENT_DELETE,
        params={"username": target.username, "role_name": await role_name(session, removed["role_id"])},
        resource_id=assignment_id,
        user_id=auth.user.id,
        old_values=removed,
        **client_info(request, auth)
    )
    await session.commit()
