from contextlib import nullcontext
from typing import List, Annotated
from fastapi import APIRouter, Depends, Request, Query
from sqlalchemy import func
from sqlmodel import select
from app.backend.utils.row_locks import lock_first
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.api.shared.helpers.db_errors import unique_violation_as
from app.backend.database import get_session
from app.backend.auth.access_control import AccessControlService
from app.backend.models.observation import Category, SignalType
from app.backend.auth.context import AuthContext
from app.backend.auth.require import require
from app.backend.enums.general import Permissions
from app.backend.enums.routes import CategoryRoutes
from app.backend.api.shared.helpers.audit_helper import client_info
from app.backend.enums.audit import AuditEvent
from app.backend.services.audit import create_audit_log
from app.backend.api.v1.models.request.categories import CategoryCreate, CategoryUpdate
from app.backend.api.v1.models.response.categories import CategoryResponse
from app.backend.api.v1.models.response.signal_types import SignalTypeResponse
from app.backend.api.v1.models.query.base import MAX_PAGE
from app.backend.api.v1.models.query.categories import CategoryFilterParams
from app.backend.filters.category_filter import CategoryFilterService
from app.backend.errors.problems import ApiError
from app.backend.enums.errors import AccessErrorCode, ErrorCode
from app.backend.errors.openapi import GUARDED, problems

router = APIRouter(prefix=CategoryRoutes.BASE.value, tags=[CategoryRoutes.tag()])


@router.get(
    CategoryRoutes.LIST_ALL.value,
    response_model=List[CategoryResponse],
    responses=problems(AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION, *GUARDED),
)
async def list_categories(
        filters: Annotated[CategoryFilterParams, Query()],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.CATEGORY_VIEW]))
):
    AccessControlService.check_include_deleted(auth, filters, Permissions.CATEGORY_ARCHIVE)
    query = select(Category)
    query = CategoryFilterService.apply_filters(query, filters)
    result = await session.exec(query)
    categories = result.all()

    return [CategoryResponse.model_validate(cat) for cat in categories]


@router.post(
    CategoryRoutes.CREATE.value,
    response_model=CategoryResponse,
    responses=problems(ErrorCode.CATEGORY_NAME_ARCHIVED, ErrorCode.CATEGORY_NAME_TAKEN, *GUARDED),
)
async def create_category(
        request: Request,
        category_data: CategoryCreate,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.CATEGORY_VIEW, Permissions.CATEGORY_CREATE]))
):
    current_user = auth.user
    existing_result = await session.exec(
        select(Category).where(
            func.lower(Category.name) == func.lower(category_data.name)
        )
    )
    existing_category = existing_result.first()

    if existing_category:
        code = ErrorCode.CATEGORY_NAME_ARCHIVED if existing_category.is_deleted else ErrorCode.CATEGORY_NAME_TAKEN
        raise ApiError(code, name=category_data.name)

    category = Category(
        name=category_data.name,
        alias=category_data.alias
    )

    session.add(category)

    # Two creates pass the SELECT check together; the unique index decides, the loser is a plain name clash
    # One transaction: the row, its audit row, one commit
    async with unique_violation_as(session, ErrorCode.CATEGORY_NAME_TAKEN, name=category_data.name):
        await session.flush()
        await session.refresh(category)
        await create_audit_log(
            session, AuditEvent.CATEGORY_CREATE,
            params={"name": category.name},
            resource_id=category.id,
            user_id=current_user.id,
            old_values=None,
            new_values=category.model_dump(),
            **client_info(request, auth)
        )
        await session.commit()

    return category


@router.get(
    CategoryRoutes.CATEGORY_BY_ID.value,
    response_model=CategoryResponse,
    responses=problems(ErrorCode.CATEGORY_NOT_FOUND, *GUARDED),
)
async def get_category(
        category_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.CATEGORY_VIEW]))
):
    result = await session.exec(
        select(Category).where(
            Category.id == category_id,
            Category.is_deleted == False
        )
    )
    category = result.first()

    if not category:
        raise ApiError(ErrorCode.CATEGORY_NOT_FOUND)

    return CategoryResponse.model_validate(category)


@router.put(
    CategoryRoutes.CATEGORY_BY_ID.value,
    response_model=CategoryResponse,
    responses=problems(
        ErrorCode.CATEGORY_NAME_ARCHIVED, ErrorCode.CATEGORY_NAME_TAKEN, ErrorCode.CATEGORY_NOT_FOUND,
        *GUARDED,
    ),
)
async def update_category(
        request: Request,
        category_id: int,
        category_data: CategoryUpdate,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.CATEGORY_VIEW, Permissions.CATEGORY_UPDATE]))
):
    current_user = auth.user
    category = await lock_first(session, select(Category).where(
        Category.id == category_id,
        Category.is_deleted == False
    ))

    if not category:
        raise ApiError(ErrorCode.CATEGORY_NOT_FOUND)

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
            code = ErrorCode.CATEGORY_NAME_ARCHIVED if existing_category.is_deleted else ErrorCode.CATEGORY_NAME_TAKEN
            raise ApiError(code, name=category_data.name)

    update_data = category_data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(category, field, value)

    session.add(category)

    # One transaction: the change, its audit row, one commit; only a new name can hit the unique index
    guard = (unique_violation_as(session, ErrorCode.CATEGORY_NAME_TAKEN, name=category_data.name)
             if "name" in update_data else nullcontext())
    async with guard:
        await session.flush()
        await session.refresh(category)
        await create_audit_log(
            session, AuditEvent.CATEGORY_UPDATE,
            params={"name": category.name},
            resource_id=category.id,
            user_id=current_user.id,
            old_values=old_values,
            new_values=category.model_dump(),
            **client_info(request, auth)
        )
        await session.commit()

    return category


@router.post(
    CategoryRoutes.CATEGORY_ARCHIVE.value,
    response_model=CategoryResponse,
    responses=problems(ErrorCode.CATEGORY_HAS_SIGNAL_TYPES, ErrorCode.CATEGORY_NOT_FOUND, *GUARDED),
)
async def archive_category(
        request: Request,
        category_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.CATEGORY_VIEW, Permissions.CATEGORY_ARCHIVE]))
):
    """Archive a category (is_deleted=true). Nothing is removed: the row is hidden from lists unless
    include_deleted=true and cannot be used for new data. Undo with POST /{category_id}/restore. Already archived or unknown: 404."""
    current_user = auth.user
    row = await lock_first(session, select(Category).where(Category.id == category_id, Category.is_deleted == False))
    if not row:
        raise ApiError(ErrorCode.CATEGORY_NOT_FOUND)

    signal_type_result = await session.exec(
        select(SignalType.id).where(SignalType.category_id == category_id, SignalType.is_deleted == False).limit(1)
    )
    if signal_type_result.first() is not None:
        raise ApiError(ErrorCode.CATEGORY_HAS_SIGNAL_TYPES)

    old_values = row.model_dump()
    row.is_deleted = True
    session.add(row)
    await create_audit_log(
        session, AuditEvent.CATEGORY_ARCHIVE,
        params={"name": row.name},
        resource_id=row.id,
        user_id=current_user.id,
        old_values=old_values,
        new_values=row.model_dump(),
        **client_info(request, auth),
    )
    await session.commit()
    await session.refresh(row)
    return CategoryResponse.model_validate(row)


@router.post(
    CategoryRoutes.CATEGORY_RESTORE.value,
    response_model=CategoryResponse,
    responses=problems(ErrorCode.CATEGORY_NOT_FOUND, ErrorCode.CATEGORY_NOT_ARCHIVED, *GUARDED),
)
async def restore_category(
        request: Request,
        category_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.CATEGORY_VIEW, Permissions.CATEGORY_ARCHIVE]))
):
    """Restore an archived category (is_deleted=false). Not archived: 409."""
    current_user = auth.user
    row = await lock_first(session, select(Category).where(Category.id == category_id))
    if not row:
        raise ApiError(ErrorCode.CATEGORY_NOT_FOUND)
    if not row.is_deleted:
        raise ApiError(ErrorCode.CATEGORY_NOT_ARCHIVED)

    old_values = row.model_dump()
    row.is_deleted = False
    session.add(row)
    await create_audit_log(
        session, AuditEvent.CATEGORY_RESTORE,
        params={"name": row.name},
        resource_id=row.id,
        user_id=current_user.id,
        old_values=old_values,
        new_values=row.model_dump(),
        **client_info(request, auth),
    )
    await session.commit()
    await session.refresh(row)
    return CategoryResponse.model_validate(row)


@router.get(
    CategoryRoutes.CATEGORY_SIGNAL_TYPES.value,
    response_model=List[SignalTypeResponse],
    responses=problems(ErrorCode.CATEGORY_NOT_FOUND, *GUARDED),
)
async def list_category_signal_types(
        category_id: int,
        page: int = Query(default=1, ge=1, le=MAX_PAGE, description="Page number"),
        page_size: int = Query(default=50, ge=1, le=200, description="Items per page"),
        include_inactive: bool = False,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.CATEGORY_VIEW, Permissions.SIGNAL_TYPE_VIEW]))
):
    category_result = await session.exec(
        select(Category).where(
            Category.id == category_id,
            Category.is_deleted == False
        )
    )
    category = category_result.first()

    if not category:
        raise ApiError(ErrorCode.CATEGORY_NOT_FOUND)

    query = select(SignalType).where(
        SignalType.category_id == category_id,
        SignalType.is_deleted == False
    )

    if not include_inactive:
        query = query.where(SignalType.is_active == True)

    result = await session.exec(query.order_by(SignalType.sort_order, SignalType.id).offset((page - 1) * page_size).limit(page_size))
    signal_types = result.all()

    return [SignalTypeResponse.model_validate(st) for st in signal_types]
