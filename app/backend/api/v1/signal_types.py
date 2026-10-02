from typing import List
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, Request
from loguru import logger
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.database import get_session
from app.backend.services.access_control import AccessControlService
from app.backend.models.observation import Category, SignalType, Observation
from app.backend.services.auth_context import AuthContext
from app.backend.services.permissions import require
from app.backend.enums.general import Permissions
from app.backend.enums.routes import SignalTypeRoutes
from app.backend.helpers.audit_helper import client_info, create_audit_log
from app.backend.api.shared.models.request.signal_type_request import SignalTypeCreate, SignalTypeUpdate
from app.backend.api.shared.models.response.signal_type_response import SignalTypeResponse, SignalTypeWithCategoryResponse
from app.backend.filters.params.signal_type_params import SignalTypeFilterParams
from app.backend.filters.signal_type_filter import SignalTypeFilterService
from app.backend.api.shared.models.response.search_response import EntitySchemaInfo
from app.backend.services.api_errors import ApiError
from app.backend.enums.errors import AccessErrorCode, ErrorCode
from app.backend.openapi_problems import ADMIN, GUARDED, problems

router = APIRouter(prefix=SignalTypeRoutes.BASE.value, tags=[SignalTypeRoutes.tag()])


@router.get(
    SignalTypeRoutes.LIST_ALL.value,
    response_model=List[SignalTypeResponse],
    responses=problems(AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION, *GUARDED),
)
async def list_signal_types(
        filters: SignalTypeFilterParams = Depends(),
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.SIGNAL_TYPE_VIEW]))
):
    AccessControlService.check_include_deleted(auth, filters, Permissions.SIGNAL_TYPE_ARCHIVE)
    query = select(SignalType)
    query = SignalTypeFilterService.apply_filters(query, filters)
    result = await session.exec(query)
    signal_types = result.all()

    return [SignalTypeResponse.model_validate(st) for st in signal_types]


@router.get(SignalTypeRoutes.FILTER_SCHEMA.value, response_model=EntitySchemaInfo, responses=problems(*GUARDED))
async def get_signal_type_filter_schema(
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.SIGNAL_TYPE_VIEW]))
):
    """Available filter params and sortable fields for signal types."""
    user_is_admin = auth.is_admin
    return EntitySchemaInfo(**SignalTypeFilterService.get_schema(is_admin=user_is_admin))


@router.post(
    SignalTypeRoutes.CREATE.value,
    response_model=SignalTypeResponse,
    responses=problems(
        ErrorCode.SIGNAL_TYPE_CODE_TAKEN, ErrorCode.CATEGORY_NOT_FOUND, ErrorCode.DATABASE_CONSTRAINT_VIOLATION, *ADMIN,
    ),
)
async def create_signal_type(
        request: Request,
        signal_type_data: SignalTypeCreate,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.SIGNAL_TYPE_VIEW, Permissions.SIGNAL_TYPE_CREATE], admin=True))
):
    current_user = auth.user
    # Validate category if provided
    if signal_type_data.category_id is not None:
        category_result = await session.exec(
            select(Category).where(
                Category.id == signal_type_data.category_id,
                Category.is_deleted == False
            )
        )
        category = category_result.first()

        if not category:
            raise ApiError(ErrorCode.CATEGORY_NOT_FOUND, category_id=signal_type_data.category_id)

    existing_result = await session.exec(
        select(SignalType).where(
            func.lower(SignalType.code) == func.lower(signal_type_data.code)
        )
    )
    existing_signal_type = existing_result.first()

    if existing_signal_type:
        if not existing_signal_type.is_deleted:
            raise ApiError(ErrorCode.SIGNAL_TYPE_CODE_TAKEN, signal_type_code=signal_type_data.code)
        
        # Restore archived signal type
        old_values = existing_signal_type.model_dump()
        
        # Update fields from request
        update_data = signal_type_data.model_dump()
        for field, value in update_data.items():
            setattr(existing_signal_type, field, value)
            
        existing_signal_type.is_deleted = False
        existing_signal_type.created_at = datetime.now(timezone.utc)
        existing_signal_type.updated_at = datetime.now(timezone.utc)
        signal_type = existing_signal_type
        action = "restore"
    else:
        signal_type = SignalType(
            code=signal_type_data.code,
            name=signal_type_data.name,
            category_id=signal_type_data.category_id,
            description=signal_type_data.description,
            is_active=signal_type_data.is_active,
            sort_order=signal_type_data.sort_order,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc)
        )
        old_values = None
        action = "create"

    session.add(signal_type)

    try:
        await session.commit()
        await session.refresh(signal_type)

        # Create audit log
        await create_audit_log(
            session=session,
            action=action,
            resource_type="signal_type",
            resource_id=signal_type.id,
            user_id=current_user.id,
            old_values=old_values,
            new_values=signal_type.model_dump(),
            details={"message": f"User {current_user.username} {action}d signal type '{signal_type.name}' ({signal_type.code})"},
            **client_info(request, auth)
        )
        await session.commit()

        return signal_type
    except IntegrityError as e:
        await session.rollback()
        logger.exception(f"Unexpected database constraint violation: {str(e.orig) if hasattr(e, 'orig') else str(e)}")
        raise ApiError(ErrorCode.DATABASE_CONSTRAINT_VIOLATION)


@router.get(
    SignalTypeRoutes.SIGNAL_TYPE_BY_ID.value,
    response_model=SignalTypeWithCategoryResponse,
    responses=problems(ErrorCode.SIGNAL_TYPE_NOT_FOUND, *GUARDED),
)
async def get_signal_type(
        signal_type_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.SIGNAL_TYPE_VIEW]))
):
    result = await session.exec(
        select(SignalType).where(
            SignalType.id == signal_type_id,
            SignalType.is_deleted == False
        ).options(selectinload(SignalType.category))
    )
    signal_type = result.first()

    if not signal_type:
        raise ApiError(ErrorCode.SIGNAL_TYPE_NOT_FOUND)

    return SignalTypeWithCategoryResponse.model_validate(signal_type)


@router.put(
    SignalTypeRoutes.SIGNAL_TYPE_BY_ID.value,
    response_model=SignalTypeResponse,
    responses=problems(
        ErrorCode.SIGNAL_TYPE_CODE_ARCHIVED, ErrorCode.SIGNAL_TYPE_CODE_TAKEN, ErrorCode.CATEGORY_NOT_FOUND,
        ErrorCode.SIGNAL_TYPE_NOT_FOUND, ErrorCode.DATABASE_CONSTRAINT_VIOLATION, *ADMIN,
    ),
)
async def update_signal_type(
        request: Request,
        signal_type_id: int,
        signal_type_data: SignalTypeUpdate,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.SIGNAL_TYPE_VIEW, Permissions.SIGNAL_TYPE_UPDATE], admin=True))
):
    current_user = auth.user
    result = await session.exec(
        select(SignalType).where(
            SignalType.id == signal_type_id,
            SignalType.is_deleted == False
        )
    )
    signal_type = result.first()

    if not signal_type:
        raise ApiError(ErrorCode.SIGNAL_TYPE_NOT_FOUND)

    # Store old values for auditing
    old_values = signal_type.model_dump()

    if signal_type_data.code is not None:
        existing_result = await session.exec(
            select(SignalType).where(
                func.lower(SignalType.code) == func.lower(signal_type_data.code),
                SignalType.id != signal_type_id
            )
        )
        existing_signal_type = existing_result.first()

        if existing_signal_type:
            code = ErrorCode.SIGNAL_TYPE_CODE_ARCHIVED if existing_signal_type.is_deleted else ErrorCode.SIGNAL_TYPE_CODE_TAKEN
            raise ApiError(code, signal_type_code=signal_type_data.code)

    if signal_type_data.category_id is not None:
        category_result = await session.exec(
            select(Category).where(
                Category.id == signal_type_data.category_id,
                Category.is_deleted == False
            )
        )
        category = category_result.first()

        if not category:
            raise ApiError(ErrorCode.CATEGORY_NOT_FOUND, category_id=signal_type_data.category_id)

    update_data = signal_type_data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(signal_type, field, value)

    signal_type.updated_at = datetime.now(timezone.utc)

    session.add(signal_type)

    try:
        await session.commit()
        await session.refresh(signal_type)

        # Create audit log
        await create_audit_log(
            session=session,
            action="update",
            resource_type="signal_type",
            resource_id=signal_type.id,
            user_id=current_user.id,
            old_values=old_values,
            new_values=signal_type.model_dump(),
            details={"message": f"User {current_user.username} updated signal type '{signal_type.name}'"},
            **client_info(request, auth)
        )
        await session.commit()

        return signal_type
    except IntegrityError as e:
        await session.rollback()
        logger.exception(f"Unexpected database constraint violation: {str(e.orig) if hasattr(e, 'orig') else str(e)}")
        raise ApiError(ErrorCode.DATABASE_CONSTRAINT_VIOLATION)


@router.post(
    SignalTypeRoutes.SIGNAL_TYPE_ARCHIVE.value,
    response_model=SignalTypeResponse,
    responses=problems(ErrorCode.SIGNAL_TYPE_HAS_OBSERVATIONS, ErrorCode.SIGNAL_TYPE_NOT_FOUND, *ADMIN),
)
async def archive_signal_type(
        request: Request,
        signal_type_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.SIGNAL_TYPE_VIEW, Permissions.SIGNAL_TYPE_ARCHIVE], admin=True))
):
    """Archive a signal type (is_deleted=true). Nothing is removed: the row is hidden from lists unless
    include_deleted=true and cannot be used for new data. Undo with POST /{signal_type_id}/restore. Already archived or unknown: 404."""
    current_user = auth.user
    result = await session.exec(select(SignalType).where(SignalType.id == signal_type_id, SignalType.is_deleted == False))
    row = result.first()
    if not row:
        raise ApiError(ErrorCode.SIGNAL_TYPE_NOT_FOUND)

    observation_result = await session.exec(
        select(Observation).where(Observation.signal_type_id == signal_type_id, Observation.is_deleted == False).limit(1)
    )
    if observation_result.first() is not None:
        raise ApiError(ErrorCode.SIGNAL_TYPE_HAS_OBSERVATIONS)

    old_values = row.model_dump()
    row.is_deleted = True
    session.add(row)
    await create_audit_log(
        session=session,
        action="archive",
        resource_type="signal_type",
        resource_id=row.id,
        user_id=current_user.id,
        old_values=old_values,
        new_values=row.model_dump(),
        details={"message": f"User {current_user.username} archived signal type '{row.name}'"},
        **client_info(request, auth),
    )
    await session.commit()
    await session.refresh(row)
    return SignalTypeResponse.model_validate(row)


@router.post(
    SignalTypeRoutes.SIGNAL_TYPE_RESTORE.value,
    response_model=SignalTypeResponse,
    responses=problems(ErrorCode.SIGNAL_TYPE_NOT_FOUND, ErrorCode.SIGNAL_TYPE_NOT_ARCHIVED, *ADMIN),
)
async def restore_signal_type(
        request: Request,
        signal_type_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.SIGNAL_TYPE_VIEW, Permissions.SIGNAL_TYPE_ARCHIVE], admin=True))
):
    """Restore an archived signal type (is_deleted=false). Not archived: 409."""
    current_user = auth.user
    result = await session.exec(select(SignalType).where(SignalType.id == signal_type_id))
    row = result.first()
    if not row:
        raise ApiError(ErrorCode.SIGNAL_TYPE_NOT_FOUND)
    if not row.is_deleted:
        raise ApiError(ErrorCode.SIGNAL_TYPE_NOT_ARCHIVED)

    old_values = row.model_dump()
    row.is_deleted = False
    session.add(row)
    await create_audit_log(
        session=session,
        action="restore",
        resource_type="signal_type",
        resource_id=row.id,
        user_id=current_user.id,
        old_values=old_values,
        new_values=row.model_dump(),
        details={"message": f"User {current_user.username} restored signal type '{row.name}'"},
        **client_info(request, auth),
    )
    await session.commit()
    await session.refresh(row)
    return SignalTypeResponse.model_validate(row)
