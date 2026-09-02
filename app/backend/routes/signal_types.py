from typing import List, Optional
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, status as fapi_status, Request
from loguru import logger
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.database import get_session
from app.backend.services.access_control import AccessControlService
from app.backend.models.observation import Category, SignalType, Observation
from app.backend.models.user import User
from app.backend.services.auth_context import AuthContext
from app.backend.routes.models.response.error_response import ACCESS_ERROR_RESPONSES
from app.backend.services.permissions import require
from app.backend.enums.general import Permissions
from app.backend.enums.routes import SignalTypeRoutes
from app.backend.helpers.audit_helper import create_audit_log
from app.backend.routes.models.request.signal_type_request import SignalTypeCreate, SignalTypeUpdate
from app.backend.routes.models.response.signal_type_response import SignalTypeResponse, SignalTypeWithCategoryResponse
from app.backend.routes.models.response.observations_response import MessageResponse
from app.backend.filters.params.signal_type_params import SignalTypeFilterParams
from app.backend.filters.signal_type_filter import SignalTypeFilterService
from app.backend.routes.models.response.search_response import EntitySchemaInfo

router = APIRouter(responses=ACCESS_ERROR_RESPONSES)


@router.get(SignalTypeRoutes.LIST_ALL.value, response_model=List[SignalTypeResponse])
async def list_signal_types(
        filters: SignalTypeFilterParams = Depends(),
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.SIGNAL_TYPE_VIEW]))
):
    AccessControlService.check_include_deleted(auth, filters, Permissions.SIGNAL_TYPE_DELETE)
    query = select(SignalType)
    query = SignalTypeFilterService.apply_filters(query, filters)
    result = await session.exec(query)
    signal_types = result.all()

    return [SignalTypeResponse.model_validate(st) for st in signal_types]


@router.get(SignalTypeRoutes.FILTER_SCHEMA.value, response_model=EntitySchemaInfo)
async def get_signal_type_filter_schema(
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.SIGNAL_TYPE_VIEW]))
):
    """Available filter params and sortable fields for signal types."""
    current_user = auth.user
    user_is_admin = auth.is_admin
    return EntitySchemaInfo(**SignalTypeFilterService.get_schema(is_admin=user_is_admin))


@router.post(SignalTypeRoutes.CREATE.value, response_model=SignalType)
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
            raise HTTPException(
                status_code=fapi_status.HTTP_404_NOT_FOUND,
                detail=f"Category with id {signal_type_data.category_id} does not exist"
            )

    existing_result = await session.exec(
        select(SignalType).where(
            func.lower(SignalType.code) == func.lower(signal_type_data.code)
        )
    )
    existing_signal_type = existing_result.first()

    if existing_signal_type:
        if not existing_signal_type.is_deleted:
            raise HTTPException(
                status_code=fapi_status.HTTP_400_BAD_REQUEST,
                detail=f"SignalType with code '{signal_type_data.code}' already exists"
            )
        
        # Restore soft-deleted signal type
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
            session_id=getattr(request.state, "session_id", None),
            old_values=old_values,
            new_values=signal_type.model_dump(),
            details={"message": f"User {current_user.username} {action}d signal type '{signal_type.name}' ({signal_type.code})"},
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_url=str(request.url)
        )
        await session.commit()

        return signal_type
    except IntegrityError as e:
        await session.rollback()
        logger.exception(f"Unexpected database constraint violation: {str(e.orig) if hasattr(e, 'orig') else str(e)}")
        raise HTTPException(
            status_code=fapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unexpected database constraint violation"
        )


@router.get(SignalTypeRoutes.SIGNAL_TYPE_BY_ID.value, response_model=SignalTypeWithCategoryResponse)
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
        raise HTTPException(
            status_code=fapi_status.HTTP_404_NOT_FOUND,
            detail="SignalType not found"
        )

    return SignalTypeWithCategoryResponse.model_validate(signal_type)


@router.put(SignalTypeRoutes.SIGNAL_TYPE_BY_ID.value, response_model=SignalType)
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
        raise HTTPException(
            status_code=fapi_status.HTTP_404_NOT_FOUND,
            detail="SignalType not found"
        )

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
            status_msg = "already exists" if not existing_signal_type.is_deleted else "already exists as a deleted signal type"
            raise HTTPException(
                status_code=fapi_status.HTTP_400_BAD_REQUEST,
                detail=f"SignalType with code '{signal_type_data.code}' {status_msg}"
            )

    if signal_type_data.category_id is not None:
        category_result = await session.exec(
            select(Category).where(
                Category.id == signal_type_data.category_id,
                Category.is_deleted == False
            )
        )
        category = category_result.first()

        if not category:
            raise HTTPException(
                status_code=fapi_status.HTTP_404_NOT_FOUND,
                detail=f"Category with id {signal_type_data.category_id} does not exist"
            )

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
            session_id=getattr(request.state, "session_id", None),
            old_values=old_values,
            new_values=signal_type.model_dump(),
            details={"message": f"User {current_user.username} updated signal type '{signal_type.name}'"},
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_url=str(request.url)
        )
        await session.commit()

        return signal_type
    except IntegrityError as e:
        await session.rollback()
        logger.exception(f"Unexpected database constraint violation: {str(e.orig) if hasattr(e, 'orig') else str(e)}")
        raise HTTPException(
            status_code=fapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unexpected database constraint violation"
        )


@router.delete(SignalTypeRoutes.SIGNAL_TYPE_BY_ID.value, response_model=MessageResponse)
async def delete_signal_type(
        request: Request,
        signal_type_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.SIGNAL_TYPE_VIEW, Permissions.SIGNAL_TYPE_DELETE], admin=True))
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
        raise HTTPException(
            status_code=fapi_status.HTTP_404_NOT_FOUND,
            detail="SignalType not found"
        )

    observation_result = await session.exec(
        select(Observation).where(
            Observation.signal_type_id == signal_type_id,
            Observation.is_deleted == False
        ).limit(1)
    )
    has_observations = observation_result.first() is not None

    if has_observations:
        raise HTTPException(
            status_code=fapi_status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete signal type that has observations"
        )

    # Store old values for auditing
    old_values = signal_type.model_dump()

    signal_type.is_deleted = True
    session.add(signal_type)
    
    # Create audit log
    await create_audit_log(
        session=session,
        action="delete",
        resource_type="signal_type",
        resource_id=signal_type.id,
        user_id=current_user.id,
        session_id=getattr(request.state, "session_id", None),
        old_values=old_values,
        new_values=signal_type.model_dump(),
        details={"message": f"User {current_user.username} deleted signal type '{signal_type.name}'"},
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        request_url=str(request.url)
    )
    
    await session.commit()

    return MessageResponse(message="SignalType deleted successfully")
