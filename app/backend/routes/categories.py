from typing import List, Optional
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, status as fapi_status
from loguru import logger
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.database import get_session
from app.backend.models.observation import Category, SignalType
from app.backend.models.user import User
from app.backend.dependencies import get_current_user
from app.backend.services.permissions import PermissionService
from app.backend.enums.general import Permissions
from app.backend.enums.routes import CategoryRoutes
from app.backend.routes.models.request.category_request import CategoryCreate, CategoryUpdate
from app.backend.routes.models.response.category_response import CategoryResponse, CategoryWithSignalTypesResponse
from app.backend.routes.models.response.signal_type_response import SignalTypeResponse
from app.backend.routes.models.response.observations_response import MessageResponse

router = APIRouter()


@router.get(CategoryRoutes.LIST_ALL.value, response_model=List[CategoryResponse])
@PermissionService.require_permission(Permissions.CATEGORY_VIEW.value)
async def list_categories(
        skip: int = 0,
        limit: int = 100,
        include_deleted: bool = False,
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
    query = select(Category)

    if not include_deleted:
        query = query.where(Category.is_deleted == False)

    result = await session.exec(query.offset(skip).limit(limit))
    categories = result.all()

    return [CategoryResponse(**cat.model_dump()) for cat in categories]


@router.post(CategoryRoutes.CREATE.value, response_model=Category)
@PermissionService.require_permission(Permissions.CATEGORY_CREATE.value)
async def create_category(
        category_data: CategoryCreate,
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
    existing_result = await session.exec(
        select(Category).where(
            func.lower(Category.name) == func.lower(category_data.name),
            Category.is_deleted == False
        )
    )
    existing_category = existing_result.first()

    if existing_category:
        raise HTTPException(
            status_code=fapi_status.HTTP_400_BAD_REQUEST,
            detail=f"Category with name '{category_data.name}' already exists"
        )

    category = Category(
        name=category_data.name,
        alias=category_data.alias
    )

    session.add(category)

    try:
        await session.commit()
        await session.refresh(category)
        return category
    except IntegrityError as e:
        await session.rollback()
        logger.exception(f"Unexpected database constraint violation: {str(e.orig) if hasattr(e, 'orig') else str(e)}")
        raise HTTPException(
            status_code=fapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unexpected database constraint violation"
        )


@router.get(CategoryRoutes.CATEGORY_BY_ID.value, response_model=CategoryResponse)
@PermissionService.require_permission(Permissions.CATEGORY_VIEW.value)
async def get_category(
        category_id: int,
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
    result = await session.exec(
        select(Category).where(
            Category.id == category_id,
            Category.is_deleted == False
        )
    )
    category = result.first()

    if not category:
        raise HTTPException(
            status_code=fapi_status.HTTP_404_NOT_FOUND,
            detail="Category not found"
        )

    return CategoryResponse(**category.model_dump())


@router.put(CategoryRoutes.CATEGORY_BY_ID.value, response_model=Category)
@PermissionService.require_permission(Permissions.CATEGORY_UPDATE.value)
async def update_category(
        category_id: int,
        category_data: CategoryUpdate,
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
    result = await session.exec(
        select(Category).where(
            Category.id == category_id,
            Category.is_deleted == False
        )
    )
    category = result.first()

    if not category:
        raise HTTPException(
            status_code=fapi_status.HTTP_404_NOT_FOUND,
            detail="Category not found"
        )

    if category_data.name is not None:
        existing_result = await session.exec(
            select(Category).where(
                func.lower(Category.name) == func.lower(category_data.name),
                Category.id != category_id,
                Category.is_deleted == False
            )
        )
        existing_category = existing_result.first()

        if existing_category:
            raise HTTPException(
                status_code=fapi_status.HTTP_400_BAD_REQUEST,
                detail=f"Category with name '{category_data.name}' already exists"
            )

    update_data = category_data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(category, field, value)

    session.add(category)

    try:
        await session.commit()
        await session.refresh(category)
        return category
    except IntegrityError as e:
        await session.rollback()
        logger.exception(f"Unexpected database constraint violation: {str(e.orig) if hasattr(e, 'orig') else str(e)}")
        raise HTTPException(
            status_code=fapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unexpected database constraint violation"
        )


@router.delete(CategoryRoutes.CATEGORY_BY_ID.value, response_model=MessageResponse)
@PermissionService.require_permission(Permissions.CATEGORY_DELETE.value)
async def delete_category(
        category_id: int,
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
    result = await session.exec(
        select(Category).where(
            Category.id == category_id,
            Category.is_deleted == False
        )
    )
    category = result.first()

    if not category:
        raise HTTPException(
            status_code=fapi_status.HTTP_404_NOT_FOUND,
            detail="Category not found"
        )

    category.is_deleted = True
    session.add(category)
    await session.commit()

    return MessageResponse(message="Category deleted successfully")


@router.get(CategoryRoutes.CATEGORY_SIGNAL_TYPES.value, response_model=List[SignalTypeResponse])
@PermissionService.require_permission(Permissions.CATEGORY_VIEW.value)
async def list_category_signal_types(
        category_id: int,
        skip: int = 0,
        limit: int = 100,
        include_inactive: bool = False,
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
    category_result = await session.exec(
        select(Category).where(
            Category.id == category_id,
            Category.is_deleted == False
        )
    )
    category = category_result.first()

    if not category:
        raise HTTPException(
            status_code=fapi_status.HTTP_404_NOT_FOUND,
            detail="Category not found"
        )

    query = select(SignalType).where(
        SignalType.category_id == category_id,
        SignalType.is_deleted == False
    )

    if not include_inactive:
        query = query.where(SignalType.is_active == True)

    result = await session.exec(query.offset(skip).limit(limit))
    signal_types = result.all()

    return [SignalTypeResponse(**st.model_dump()) for st in signal_types]
