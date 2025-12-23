from typing import List, Optional
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, status as fapi_status
from loguru import logger
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.database import get_session
from app.backend.models.observation import Category, SignalType, Observation
from app.backend.models.user import User
from app.backend.dependencies import get_current_user
from app.backend.services.permissions import PermissionService
from app.backend.enums.general import Permissions
from app.backend.enums.routes import SignalTypeRoutes
from app.backend.routes.models.request.signal_type_request import SignalTypeCreate, SignalTypeUpdate
from app.backend.routes.models.response.signal_type_response import SignalTypeResponse, SignalTypeWithCategoryResponse
from app.backend.routes.models.response.observations_response import MessageResponse

router = APIRouter()


@router.get(SignalTypeRoutes.LIST_ALL.value, response_model=List[SignalTypeResponse])
@PermissionService.require_permission(Permissions.SIGNAL_TYPE_VIEW.value)
async def list_signal_types(
        skip: int = 0,
        limit: int = 100,
        category_id: Optional[int] = None,
        include_inactive: bool = False,
        include_deleted: bool = False,
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
    query = select(SignalType)

    if not include_deleted:
        query = query.where(SignalType.is_deleted == False)

    if category_id is not None:
        query = query.where(SignalType.category_id == category_id)

    if not include_inactive:
        query = query.where(SignalType.is_active == True)

    result = await session.exec(query.offset(skip).limit(limit))
    signal_types = result.all()

    return [SignalTypeResponse(**st.model_dump()) for st in signal_types]


@router.post(SignalTypeRoutes.CREATE.value, response_model=SignalType)
@PermissionService.require_permission(Permissions.SIGNAL_TYPE_CREATE.value)
async def create_signal_type(
        signal_type_data: SignalTypeCreate,
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
    existing_result = await session.exec(
        select(SignalType).where(
            func.lower(SignalType.code) == func.lower(signal_type_data.code),
            SignalType.is_deleted == False
        )
    )
    existing_signal_type = existing_result.first()

    if existing_signal_type:
        raise HTTPException(
            status_code=fapi_status.HTTP_400_BAD_REQUEST,
            detail=f"SignalType with code '{signal_type_data.code}' already exists"
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

    session.add(signal_type)

    try:
        await session.commit()
        await session.refresh(signal_type)
        return signal_type
    except IntegrityError as e:
        await session.rollback()
        logger.exception(f"Unexpected database constraint violation: {str(e.orig) if hasattr(e, 'orig') else str(e)}")
        raise HTTPException(
            status_code=fapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unexpected database constraint violation"
        )


@router.get(SignalTypeRoutes.SIGNAL_TYPE_BY_ID.value, response_model=SignalTypeWithCategoryResponse)
@PermissionService.require_permission(Permissions.SIGNAL_TYPE_VIEW.value)
async def get_signal_type(
        signal_type_id: int,
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
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

    category_name = None
    if signal_type.category_id:
        category_result = await session.exec(
            select(Category).where(Category.id == signal_type.category_id)
        )
        category = category_result.first()
        if category:
            category_name = category.name

    response_data = signal_type.model_dump()
    response_data['category_name'] = category_name

    return SignalTypeWithCategoryResponse(**response_data)


@router.put(SignalTypeRoutes.SIGNAL_TYPE_BY_ID.value, response_model=SignalType)
@PermissionService.require_permission(Permissions.SIGNAL_TYPE_UPDATE.value)
async def update_signal_type(
        signal_type_id: int,
        signal_type_data: SignalTypeUpdate,
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
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
        return signal_type
    except IntegrityError as e:
        await session.rollback()
        logger.exception(f"Unexpected database constraint violation: {str(e.orig) if hasattr(e, 'orig') else str(e)}")
        raise HTTPException(
            status_code=fapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unexpected database constraint violation"
        )


@router.delete(SignalTypeRoutes.SIGNAL_TYPE_BY_ID.value, response_model=MessageResponse)
@PermissionService.require_permission(Permissions.SIGNAL_TYPE_DELETE.value)
async def delete_signal_type(
        signal_type_id: int,
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
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
            detail="Cannot delete signal type that has active observations"
        )

    signal_type.is_deleted = True
    session.add(signal_type)
    await session.commit()

    return MessageResponse(message="SignalType deleted successfully")
