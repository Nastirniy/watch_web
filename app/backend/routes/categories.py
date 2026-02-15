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
from app.backend.models.observation import Category, SignalType
from app.backend.models.user import User
from app.backend.dependencies import get_current_user
from app.backend.services.permissions import PermissionService
from app.backend.enums.general import Permissions
from app.backend.enums.routes import CategoryRoutes
from app.backend.helpers.audit_helper import create_audit_log
from app.backend.routes.models.request.category_request import CategoryCreate, CategoryUpdate
from app.backend.routes.models.response.category_response import CategoryResponse, CategoryWithSignalTypesResponse
from app.backend.routes.models.response.signal_type_response import SignalTypeResponse
from app.backend.routes.models.response.observations_response import MessageResponse
from app.backend.filters.params.category_params import CategoryFilterParams
from app.backend.filters.category_filter import CategoryFilterService
from app.backend.routes.models.response.search_response import EntitySchemaInfo

router = APIRouter()


@router.get(CategoryRoutes.LIST_ALL.value, response_model=List[CategoryResponse])
@PermissionService.require_permissions({"all": [
    Permissions.CATEGORY_VIEW.value,
    Permissions.IS_ADMIN.value]})
async def list_categories(
        filters: CategoryFilterParams = Depends(),
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
    query = select(Category)
    query = CategoryFilterService.apply_filters(query, filters)
    result = await session.exec(query)
    categories = result.all()

    return [CategoryResponse.model_validate(cat) for cat in categories]


@router.get(CategoryRoutes.FILTER_SCHEMA.value, response_model=EntitySchemaInfo)
@PermissionService.require_permissions({"all": [
    Permissions.CATEGORY_VIEW.value,
    Permissions.IS_ADMIN.value]})
async def get_category_filter_schema(
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
    """Available filter params and sortable fields for categories."""
    return EntitySchemaInfo(**CategoryFilterService.get_schema(is_admin=True))


@router.post(CategoryRoutes.CREATE.value, response_model=Category)
@PermissionService.require_permissions({"all": [
    Permissions.CATEGORY_CREATE.value,
    Permissions.IS_ADMIN.value]})
async def create_category(
        request: Request,
        category_data: CategoryCreate,
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
    existing_result = await session.exec(
        select(Category).where(
            func.lower(Category.name) == func.lower(category_data.name)
        )
    )
    existing_category = existing_result.first()

    if existing_category:
        if not existing_category.is_deleted:
            raise HTTPException(
                status_code=fapi_status.HTTP_400_BAD_REQUEST,
                detail=f"Category with name '{category_data.name}' already exists"
            )
        
        # Restore soft-deleted category
        old_values = existing_category.model_dump()
        existing_category.is_deleted = False
        existing_category.alias = category_data.alias
        category = existing_category
        action = "restore"
    else:
        category = Category(
            name=category_data.name,
            alias=category_data.alias
        )
        old_values = None
        action = "create"

    session.add(category)

    try:
        await session.commit()
        await session.refresh(category)

        # Create audit log
        await create_audit_log(
            session=session,
            action=action,
            resource_type="category",
            resource_id=category.id,
            user_id=current_user.id,
            session_id=getattr(request.state, "session_id", None),
            old_values=old_values,
            new_values=category.model_dump(),
            details={"message": f"User {current_user.username} {action}d category '{category.name}'"},
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_url=str(request.url)
        )
        await session.commit()

        return category
    except IntegrityError as e:
        await session.rollback()
        logger.exception(f"Unexpected database constraint violation: {str(e.orig) if hasattr(e, 'orig') else str(e)}")
        raise HTTPException(
            status_code=fapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unexpected database constraint violation"
        )


@router.get(CategoryRoutes.CATEGORY_BY_ID.value, response_model=CategoryResponse)
@PermissionService.require_permissions({"all": [
    Permissions.CATEGORY_VIEW.value,
    Permissions.IS_ADMIN.value]})
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

    return CategoryResponse.model_validate(category)


@router.put(CategoryRoutes.CATEGORY_BY_ID.value, response_model=Category)
@PermissionService.require_permissions({"all": [
    Permissions.CATEGORY_UPDATE.value,
    Permissions.IS_ADMIN.value]})
async def update_category(
        request: Request,
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

    # Store old values for auditing
    old_values = category.model_dump()

    if category_data.name is not None:
        existing_result = await session.exec(
            select(Category).where(
                func.lower(Category.name) == func.lower(category_data.name),
                Category.id != category_id
            )
        )
        existing_category = existing_result.first()

        if existing_category:
            status_msg = "already exists" if not existing_category.is_deleted else "already exists as a deleted category"
            raise HTTPException(
                status_code=fapi_status.HTTP_400_BAD_REQUEST,
                detail=f"Category with name '{category_data.name}' {status_msg}"
            )

    update_data = category_data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(category, field, value)

    session.add(category)

    try:
        await session.commit()
        await session.refresh(category)

        # Create audit log
        await create_audit_log(
            session=session,
            action="update",
            resource_type="category",
            resource_id=category.id,
            user_id=current_user.id,
            session_id=getattr(request.state, "session_id", None),
            old_values=old_values,
            new_values=category.model_dump(),
            details={"message": f"User {current_user.username} updated category '{category.name}'"},
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_url=str(request.url)
        )
        await session.commit()

        return category
    except IntegrityError as e:
        await session.rollback()
        logger.exception(f"Unexpected database constraint violation: {str(e.orig) if hasattr(e, 'orig') else str(e)}")
        raise HTTPException(
            status_code=fapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unexpected database constraint violation"
        )


@router.delete(CategoryRoutes.CATEGORY_BY_ID.value, response_model=MessageResponse)
@PermissionService.require_permissions({"all": [
    Permissions.CATEGORY_CREATE.value,
    Permissions.IS_ADMIN.value]})
async def delete_category(
        request: Request,
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

    # Store old values for auditing
    old_values = category.model_dump()

    category.is_deleted = True
    session.add(category)
    
    # Create audit log
    await create_audit_log(
        session=session,
        action="delete",
        resource_type="category",
        resource_id=category.id,
        user_id=current_user.id,
        session_id=getattr(request.state, "session_id", None),
        old_values=old_values,
        new_values=category.model_dump(),
        details={"message": f"User {current_user.username} deleted category '{category.name}'"},
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        request_url=str(request.url)
    )
    
    await session.commit()

    return MessageResponse(message="Category deleted successfully")


@router.get(CategoryRoutes.CATEGORY_SIGNAL_TYPES.value, response_model=List[SignalTypeResponse])
@PermissionService.require_permissions({"all": [
    Permissions.CATEGORY_VIEW.value,
    Permissions.IS_ADMIN.value]})
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

    return [SignalTypeResponse.model_validate(st) for st in signal_types]
