from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status, Query, File, UploadFile, Form, Request, Response
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlalchemy.exc import IntegrityError, DataError
from sqlmodel import select, desc
from datetime import datetime, timezone
import json

from app.backend.database import get_session
from app.backend.models.user import User
from app.backend.models.role import Role, UserRole
from app.backend.models.post import Post
from app.backend.models.user_post_access import UserPostAccess
from app.backend.services.auth_context import AuthContext
from app.backend.routes.models.response.error_response import ACCESS_ERROR_RESPONSES
from app.backend.services.permissions import require
from app.backend.services.access_control import AccessControlService
from app.backend.services.auth import AuthService
from app.backend.helpers.certeficates_helper import CertificateParser, hash_data, hash_pki_certificate_data
from app.backend.enums.general import Permissions
from app.backend.enums.routes import UserRoutes, SystemRoutes
from app.backend.helpers.audit_helper import create_audit_log
from app.backend.utils.validators import PaginationParams
from app.backend.utils.logging_config import get_logger, get_api_logger
from app.backend.routes.models.request.user_request import UserCreateData, UserUpdateData, UserPostAccessUpsert
from app.backend.routes.models.response.user_response import UserResponse, UserRoleResponse, UserPostAccessResponse
from app.backend.filters.params.user_params import UserFilterParams
from app.backend.filters.user_filter import UserFilterService
from app.backend.routes.models.response.search_response import EntitySchemaInfo

router = APIRouter(responses=ACCESS_ERROR_RESPONSES)
_logger = get_api_logger()


@router.get(SystemRoutes.ROOT.value, response_model=List[UserResponse])
async def list_users(
        filters: UserFilterParams = Depends(),
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.USER_VIEW]))
):
    """
    List users with filters.

    Users are limited to the caller's post scope (home post, extra post access, or every post with post.access_all).
    """
    current_user = auth.user
    try:
        AccessControlService.check_include_deleted(auth, filters, Permissions.USER_DELETE)
        scope = AccessControlService.resolve_post_scope(auth, filters.post_id)

        query = select(User)
        query = UserFilterService.apply_filters(query, filters, scope)
        result = await session.exec(query)
        users = result.all()

        _logger.info(f"User {current_user.username} listed {len(users)} users (scope={sorted(scope) if scope is not None else 'all'})")

        return [UserResponse.model_validate(user) for user in users]

    except HTTPException:
        raise
    except Exception as e:
        _logger.error(f"Error listing users: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve users"
        )


@router.post(SystemRoutes.ROOT.value, response_model=UserResponse)
async def create_user(
        request: Request,
        certificate: UploadFile = File(..., description="PKI certificate file (PEM or DER format)"),
        username: str = Form(..., min_length=3, max_length=50),
        username_alias: str = Form(..., min_length=1, max_length=255),
        role_ids: str = Form(..., description="JSON array of role IDs, e.g. [1, 2, 3]"),
        post_id: Optional[int] = Form(default=None),
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

    Parameters:
    - certificate: PKI certificate file (PEM or DER format)
    - username: Unique username (3-50 characters)
    - username_alias: Display name (1-255 characters)
    - role_ids: JSON array string of role IDs, e.g. "[1, 2, 3]"
    - post_id: Optional post/position ID
    """
    current_user = auth.user
    # Parse role_ids from JSON string
    try:
        _logger.debug(f"Received role_ids string: {role_ids}")
        role_ids_list = json.loads(role_ids)

        if not isinstance(role_ids_list, list):
            raise ValueError("role_ids must be a JSON array")

    except json.JSONDecodeError as e:
        _logger.error(f"JSON decode error for role_ids: {str(e)}, received: {role_ids}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid role_ids format. Must be a JSON array like [1, 2, 3]. Received: {role_ids}"
        )

    # Validate all input data using Pydantic model
    try:
        user_data = UserCreateData(
            username=username,
            username_alias=username_alias,
            post_id=post_id,
            role_ids=role_ids_list
        )
    except Exception as e:
        _logger.error(f"Validation error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Server side error"  # TODO swap to real error message
        )

    # Validate post_id exists if provided
    if user_data.post_id is not None:
        post_result = await session.exec(
            select(Post).where(Post.id == user_data.post_id)
        )
        if not post_result.first():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Post with ID {user_data.post_id} not found"
            )

    # Check if user already exists
    result = await session.exec(
        select(User).where(User.username == user_data.username)
    )
    if result.first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User with this username already exists"
        )

    # Read and parse certificate
    try:
        cert_data = await certificate.read()
        if len(cert_data) == 0:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Certificate file is empty"
            )

        # Limit certificate file size (5MB)
        max_cert_size = 5 * 1024 * 1024
        if len(cert_data) > max_cert_size:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail="Certificate file too large (max 5MB)"
            )

        # Parse certificate and extract PKI fields
        cert_info = CertificateParser.parse_certificate(cert_data)
        pki_cn = cert_info["pki_certificate_cn"]
        pki_serial = cert_info["pki_certificate_serial"]
        pki_fingerprint = cert_info["pki_certificate_fingerprint"]

        _logger.info(f"Certificate parsed successfully for user {username}: CN={pki_cn}, Serial={pki_serial[:16]}...")

    except ValueError as e:
        _logger.error(f"Certificate parsing error for user {username}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid certificate"
        )
    except Exception as e:
        _logger.error(f"Unexpected error reading certificate: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to process certificate file"
        )

    # Validate all roles exist
    roles_to_assign = []
    for role_id in user_data.role_ids:
        role_result = await session.exec(
            select(Role).where(Role.id == role_id)
        )
        role = role_result.first()
        if not role:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Role with ID {role_id} not found"
            )
        if role.is_deleted:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Role with ID {role_id} is deleted and cannot be assigned"
            )
        roles_to_assign.append(role)

    # Hash PKI certificate data before storing
    pki_cn_hash, pki_serial_hash, pki_fingerprint_hash = await hash_pki_certificate_data(
        pki_cn, pki_serial, pki_fingerprint
    )

    # Check for duplicate certificate before creating user
    duplicate_check = await session.exec(
        select(User).where(
            (User.pki_certificate_cn_hash == pki_cn_hash) |
            (User.pki_certificate_serial_hash == pki_serial_hash) |
            (User.pki_certificate_fingerprint_hash == pki_fingerprint_hash)
        )
    )
    if duplicate_check.first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Certificate already exists for another user"
        )

    # Create user with validated data and hashed PKI data
    user = User(
        username=user_data.username,
        username_alias=user_data.username_alias,
        post_id=user_data.post_id,
        pki_certificate_cn_hash=pki_cn_hash,
        pki_certificate_serial_hash=pki_serial_hash,
        pki_certificate_fingerprint_hash=pki_fingerprint_hash
    )
    session.add(user)

    try:
        await session.commit()
        await session.refresh(user)

        # Assign roles
        for role in roles_to_assign:
            user_role = UserRole(user_id=user.id, role_id=role.id)
            session.add(user_role)

        await session.commit()
        
        # Create audit log
        await create_audit_log(
            session=session,
            action="create",
            resource_type="user",
            resource_id=user.id,
            user_id=current_user.id,
            session_id=getattr(request.state, "session_id", None),
            new_values=user.model_dump(exclude={"pki_certificate_cn_hash", "pki_certificate_serial_hash", "pki_certificate_fingerprint_hash"}),
            details={"message": f"User {current_user.username} created new user {user.username} with {len(roles_to_assign)} roles"},
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_url=str(request.url)
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
        error_msg = str(e)
        if "Data too long" in error_msg:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="Data too long for one or more fields"
            )
        elif "Duplicate entry" in error_msg or "already exists" in error_msg:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="User with this username or certificate already exists"
            )
        else:
            _logger.error(f"Error creating user: {error_msg}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Database error occurred"
            )


@router.get(UserRoutes.FILTER_SCHEMA.value, response_model=EntitySchemaInfo)
async def get_user_filter_schema(
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.USER_VIEW]))
):
    """Available filter params and sortable fields for users."""
    current_user = auth.user
    user_is_admin = auth.is_admin
    return EntitySchemaInfo(**UserFilterService.get_schema(is_admin=user_is_admin))


@router.get(UserRoutes.USER_BY_ID.value, response_model=UserResponse)
async def get_user(
        user_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.USER_VIEW]))
):
    """Get user by ID - visible to the user itself and to callers whose post scope covers the user's post"""
    current_user = auth.user
    if user_id <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User ID must be a positive integer"
        )

    result = await session.exec(
        select(User).where(User.id == user_id)
        # select(User).where(User.id == user_id, User.is_deleted is False)  # TODO: implement boolean check
    )
    user = result.first()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

    AccessControlService.check_user_visibility(auth, user)

    return UserResponse.model_validate(user)


@router.patch(UserRoutes.USER_BY_ID.value, response_model=UserResponse)
async def update_user(
        request: Request,
        user_id: int,
        certificate: Optional[UploadFile] = File(None, description="Optional PKI certificate file to update (PEM or DER format)"),
        username: Optional[str] = Form(None, min_length=3, max_length=50),
        username_alias: Optional[str] = Form(None, min_length=1, max_length=255),
        post_id: Optional[int] = Form(None),
        is_active: Optional[bool] = Form(None),
        is_deleted: Optional[bool] = Form(None, description="false restores a soft-deleted user, true soft-deletes it (requires user.delete)"),
        role_ids: Optional[str] = Form(None, description="Optional JSON array of role IDs, e.g. [1, 2, 3]"),
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.USER_VIEW, Permissions.USER_UPDATE], admin=True))
):
    """
    Update user fields - all fields are optional

    Supports updating:
    - username: Unique username (3-50 characters)
    - username_alias: User's display name (1-255 characters)
    - post_id: User's post/position ID
    - is_active: User's active status (boolean)
    - role_ids: Replace all user roles with provided role IDs (JSON array string, e.g. "[1, 2, 3]")
    - certificate: Update PKI certificate authentication data (file upload)

    Only provided fields will be updated.
    """
    current_user = auth.user
    if user_id <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User ID must be a positive integer"
        )

    # Find user
    result = await session.exec(
        select(User).where(User.id == user_id)
    )
    user = result.first()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )
    if user.is_deleted and is_deleted is not False:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    # Store old values for auditing
    old_values = user.model_dump(exclude={"pki_certificate_cn_hash", "pki_certificate_serial_hash", "pki_certificate_fingerprint_hash"})

    # Parse role_ids from JSON string if provided
    role_ids_list = None
    if role_ids is not None:
        try:
            _logger.debug(f"Received role_ids string for update: {role_ids}")
            role_ids_list = json.loads(role_ids)

            if not isinstance(role_ids_list, list):
                raise ValueError("role_ids must be a JSON array")

        except json.JSONDecodeError as e:
            _logger.error(f"JSON decode error for role_ids: {str(e)}, received: {role_ids}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid role_ids format. Must be a JSON array like [1, 2, 3]. Received: {role_ids}"
            )

    # Validate input data using Pydantic model
    try:
        update_data = UserUpdateData(
            username=username,
            username_alias=username_alias,
            post_id=post_id,
            is_active=is_active,
            is_deleted=is_deleted,
            role_ids=role_ids_list
        )
    except Exception as e:
        _logger.error(f"Validation error: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Server side error"  # TODO swap to real error message
        )

    if update_data.username is not None:
        existing_username = await session.exec(
            select(User).where(User.username == update_data.username, User.id != user_id)
        )
        if existing_username.first():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="User with this username already exists"
            )
        user.username = update_data.username

    if update_data.post_id is not None:
        post_result = await session.exec(
            select(Post).where(Post.id == update_data.post_id)
        )
        if not post_result.first():
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Post with ID {update_data.post_id} not found"
            )
        user.post_id = update_data.post_id

    if update_data.username_alias is not None:
        user.username_alias = update_data.username_alias

    if update_data.is_deleted is not None:
        AccessControlService.check_permission(auth, Permissions.USER_DELETE, "changing is_deleted")
        if update_data.is_deleted and user.id == current_user.id:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot delete your own account")
        user.is_deleted = update_data.is_deleted
        if update_data.is_deleted:
            user.is_active = False
            await AuthService.invalidate_all_user_sessions(
                session=session,
                user_id=user_id,
                reason="account_deleted",
                actor_user_id=current_user.id,
                ip_address=request.client.host if request.client else None,
                user_agent=request.headers.get("user-agent")
            )
        else:
            user.is_active = True

    if update_data.is_active is not None:
        user.is_active = update_data.is_active

        # Immediately invalidate all sessions when deactivating a user
        if not update_data.is_active:
            invalidated_count = await AuthService.invalidate_all_user_sessions(
                session=session,
                user_id=user_id,
                reason="account_deactivated",
                actor_user_id=current_user.id,
                ip_address=request.client.host if request.client else None,
                user_agent=request.headers.get("user-agent")
            )
            _logger.info(
                f"User {current_user.username} deactivated user {user.username}, "
                f"invalidated {invalidated_count} active sessions"
            )

    # Handle certificate update if provided
    if certificate is not None:
        try:
            cert_data = await certificate.read()
            if len(cert_data) == 0:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Certificate file is empty"
                )

            # Limit certificate file size (5MB)
            max_cert_size = 5 * 1024 * 1024
            if len(cert_data) > max_cert_size:
                raise HTTPException(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail="Certificate file too large (max 5MB)"
                )

            # Parse certificate and extract PKI fields
            cert_info = CertificateParser.parse_certificate(cert_data)
            pki_cn = cert_info["pki_certificate_cn"]
            pki_serial = cert_info["pki_certificate_serial"]
            pki_fingerprint = cert_info["pki_certificate_fingerprint"]

            _logger.info(f"Certificate parsed for user {user.username}: CN={pki_cn}, Serial={pki_serial[:16]}...")

            # Hash PKI certificate data before storing
            pki_cn_hash = await hash_data(pki_cn)
            pki_serial_hash = await hash_data(pki_serial)
            pki_fingerprint_hash = await hash_data(pki_fingerprint)

            # Check for duplicate certificate (excluding current user)
            duplicate_check = await session.exec(
                select(User).where(
                    (
                        (User.pki_certificate_cn_hash == pki_cn_hash) |
                        (User.pki_certificate_serial_hash == pki_serial_hash) |
                        (User.pki_certificate_fingerprint_hash == pki_fingerprint_hash)
                    ) &
                    (User.id != user_id)
                )
            )
            if duplicate_check.first():
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="Certificate already exists for another user"
                )

            user.pki_certificate_cn_hash = pki_cn_hash
            user.pki_certificate_serial_hash = pki_serial_hash
            user.pki_certificate_fingerprint_hash = pki_fingerprint_hash

            _logger.info(f"User {current_user.username} updated certificate for user {user.username}")

        except ValueError as e:
            _logger.error(f"Certificate parsing error for user {user.username}: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid certificate"
            )
        except Exception as e:
            _logger.error(f"Unexpected error reading certificate: {str(e)}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to process certificate file"
            )

    # Update timestamp
    user.updated_at = datetime.now(timezone.utc)
    session.add(user)

    try:
        await session.commit()

        # Handle role updates if provided
        if update_data.role_ids is not None:
            # Validate all roles exist
            roles_to_assign = []
            for role_id in update_data.role_ids:
                role_result = await session.exec(
                    select(Role).where(Role.id == role_id)
                )
                role = role_result.first()
                if not role:
                    await session.rollback()
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"Role with ID {role_id} not found"
                    )
                if role.is_deleted:
                    await session.rollback()
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Role with ID {role_id} is deleted and cannot be assigned"
                    )
                roles_to_assign.append(role)

            # Delete existing role assignments
            existing_assignments_result = await session.exec(
                select(UserRole).where(UserRole.user_id == user_id)
            )
            existing_assignments = existing_assignments_result.all()

            for assignment in existing_assignments:
                await session.delete(assignment)

            # Assign new roles
            for role in roles_to_assign:
                user_role = UserRole(user_id=user_id, role_id=role.id)
                session.add(user_role)

            await session.commit()
            _logger.info(f"User {current_user.username} updated user {user.username} and replaced roles with {len(roles_to_assign)} new roles")

        await session.refresh(user)
        
        # Create audit log
        await create_audit_log(
            session=session,
            action="update",
            resource_type="user",
            resource_id=user.id,
            user_id=current_user.id,
            session_id=getattr(request.state, "session_id", None),
            old_values=old_values,
            new_values=user.model_dump(exclude={"pki_certificate_cn_hash", "pki_certificate_serial_hash", "pki_certificate_fingerprint_hash"}),
            details={"message": f"User {current_user.username} updated user {user.username}"},
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_url=str(request.url)
        )
        await session.commit()
        
        _logger.info(f"User {current_user.username} updated user {user.username}")

        return UserResponse.model_validate(user)

    except (IntegrityError, DataError) as e:
        await session.rollback()
        error_msg = str(e)
        if "Duplicate entry" in error_msg or "already exists" in error_msg:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Certificate already exists for another user"
            )
        else:
            _logger.error(f"Error updating user: {error_msg}")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Database error occurred"
            )


# UserRole Management Routes

@router.get(UserRoutes.USER_ROLES.value, response_model=List[UserRoleResponse])
async def list_user_roles(
        user_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.USER_VIEW, Permissions.ROLE_VIEW], admin=True))
):
    """List all roles assigned to a user - requires both USER_VIEW and ROLE_VIEW permissions"""
    current_user = auth.user
    if user_id <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User ID must be a positive integer"
        )

    # Check if user exists
    user_result = await session.exec(
        select(User).where(User.id == user_id, User.is_deleted == False)
    )
    user = user_result.first()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found"
        )

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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return user


@router.get(UserRoutes.USER_POST_ACCESS.value, response_model=List[UserPostAccessResponse])
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


@router.put(UserRoutes.USER_POST_ACCESS_BY_POST.value, response_model=UserPostAccessResponse)
async def upsert_user_post_access(
        request: Request,
        user_id: int,
        post_id: int,
        access_data: UserPostAccessUpsert,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.USER_VIEW, Permissions.USER_UPDATE, Permissions.POST_VIEW], admin=True))
):
    """Grant a user read (can_write=false) or read+write (can_write=true) access to an additional post"""
    current_user = auth.user
    user = await _get_active_user_or_404(session, user_id)

    post = await session.get(Post, post_id)
    if not post:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post not found")

    if user.post_id == post_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This post is the home post of the user and already grants full access"
        )

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

    await session.commit()
    await session.refresh(row)

    await create_audit_log(
        session=session,
        action=action,
        resource_type="user_post_access",
        resource_id=row.id,
        user_id=current_user.id,
        session_id=getattr(request.state, "session_id", None),
        old_values=old_values,
        new_values=row.model_dump(),
        details={"message": f"User {current_user.username} granted post {post_id} access to user {user.username} (can_write={row.can_write})"},
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        request_url=str(request.url)
    )
    await session.commit()

    _logger.info(f"User {current_user.username} set post access user={user_id} post={post_id} can_write={row.can_write}")
    return UserPostAccessResponse.model_validate(row)


@router.delete(UserRoutes.USER_POST_ACCESS_BY_POST.value, status_code=status.HTTP_204_NO_CONTENT)
async def delete_user_post_access(
        request: Request,
        user_id: int,
        post_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.USER_VIEW, Permissions.USER_UPDATE, Permissions.POST_VIEW], admin=True))
):
    """Revoke access of a user to an additional post"""
    current_user = auth.user
    user = await _get_active_user_or_404(session, user_id)

    result = await session.exec(
        select(UserPostAccess).where(UserPostAccess.user_id == user_id, UserPostAccess.post_id == post_id)
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Post access not found")

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
        session_id=getattr(request.state, "session_id", None),
        old_values=old_values,
        details={"message": f"User {current_user.username} revoked post {post_id} access from user {user.username}"},
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        request_url=str(request.url)
    )
    await session.commit()

    _logger.info(f"User {current_user.username} revoked post access user={user_id} post={post_id}")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.delete(UserRoutes.USER_BY_ID.value, status_code=status.HTTP_204_NO_CONTENT)
async def delete_user(
        request: Request,
        user_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.USER_VIEW, Permissions.USER_DELETE], admin=True))
):
    """Soft-delete a user: marks it deleted, deactivates it and invalidates its sessions.
    Observations and media of the user stay. Restore with PATCH is_deleted=false."""
    current_user = auth.user
    user = await _get_active_user_or_404(session, user_id)
    if user.id == current_user.id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot delete your own account")

    old_values = user.model_dump(exclude={"pki_certificate_cn_hash", "pki_certificate_serial_hash", "pki_certificate_fingerprint_hash"})
    user.is_deleted = True
    user.is_active = False
    user.updated_at = datetime.now(timezone.utc)
    session.add(user)
    await session.commit()

    invalidated = await AuthService.invalidate_all_user_sessions(
        session=session,
        user_id=user_id,
        reason="account_deleted",
        actor_user_id=current_user.id,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent")
    )
    await create_audit_log(
        session=session,
        action="delete",
        resource_type="user",
        resource_id=user.id,
        user_id=current_user.id,
        session_id=getattr(request.state, "session_id", None),
        old_values=old_values,
        details={"message": f"User {current_user.username} soft-deleted user {user.username} ({invalidated} sessions invalidated)"},
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        request_url=str(request.url)
    )
    await session.commit()
    _logger.info(f"User {current_user.username} soft-deleted user {user.username}")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
