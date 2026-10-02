from typing import List, Annotated
from fastapi import APIRouter, Depends, Request, Query
from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.database import get_session
from app.backend.services.access_control import AccessControlService
from app.backend.models.observation import Category, SignalType
from app.backend.services.auth_context import AuthContext
from app.backend.services.permissions import require
from app.backend.enums.general import Permissions
from app.backend.enums.routes import CategoryRoutes
from app.backend.helpers.audit_helper import client_info, create_audit_log
from app.backend.api.shared.models.request.category_request import CategoryCreate, CategoryUpdate
from app.backend.api.shared.models.response.category_response import CategoryResponse
from app.backend.api.shared.models.response.signal_type_response import SignalTypeResponse
from app.backend.filters.params.category_params import CategoryFilterParams
from app.backend.filters.category_filter import CategoryFilterService
from app.backend.services.api_errors import ApiError
from app.backend.enums.errors import AccessErrorCode, ErrorCode
from app.backend.openapi_problems import ADMIN, GUARDED, problems

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
    responses=problems(ErrorCode.CATEGORY_NAME_TAKEN, *ADMIN),
)
async def create_category(
        request: Request,
        category_data: CategoryCreate,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.CATEGORY_VIEW, Permissions.CATEGORY_CREATE], admin=True))
):
    current_user = auth.user
    existing_result = await session.exec(
        select(Category).where(
            func.lower(Category.name) == func.lower(category_data.name)
        )
    )
    existing_category = existing_result.first()

    if existing_category:
        if not existing_category.is_deleted:
            raise ApiError(ErrorCode.CATEGORY_NAME_TAKEN, name=category_data.name)
        
        # Restore archived category
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

    await session.commit()
    await session.refresh(category)

    # Create audit log
    await create_audit_log(
        session=session,
        action=action,
        resource_type="category",
        resource_id=category.id,
        user_id=current_user.id,
        old_values=old_values,
        new_values=category.model_dump(),
        details={"message": f"User {current_user.username} {action}d category '{category.name}'"},
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
        *ADMIN,
    ),
)
async def update_category(
        request: Request,
        category_id: int,
        category_data: CategoryUpdate,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.CATEGORY_VIEW, Permissions.CATEGORY_UPDATE], admin=True))
):
    current_user = auth.user
    result = await session.exec(
        select(Category).where(
            Category.id == category_id,
            Category.is_deleted == False
        )
    )
    category = result.first()

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

    await session.commit()
    await session.refresh(category)

    # Create audit log
    await create_audit_log(
        session=session,
        action="update",
        resource_type="category",
        resource_id=category.id,
        user_id=current_user.id,
        old_values=old_values,
        new_values=category.model_dump(),
        details={"message": f"User {current_user.username} updated category '{category.name}'"},
        **client_info(request, auth)
    )
    await session.commit()

    return category


@router.post(
    CategoryRoutes.CATEGORY_ARCHIVE.value,
    response_model=CategoryResponse,
    responses=problems(ErrorCode.CATEGORY_NOT_FOUND, *ADMIN),
)
async def archive_category(
        request: Request,
        category_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.CATEGORY_VIEW, Permissions.CATEGORY_ARCHIVE], admin=True))
):
    """Archive a category (is_deleted=true). Nothing is removed: the row is hidden from lists unless
    include_deleted=true and cannot be used for new data. Undo with POST /{category_id}/restore. Already archived or unknown: 404."""
    current_user = auth.user
    result = await session.exec(select(Category).where(Category.id == category_id, Category.is_deleted == False))
    row = result.first()
    if not row:
        raise ApiError(ErrorCode.CATEGORY_NOT_FOUND)

    old_values = row.model_dump()
    row.is_deleted = True
    session.add(row)
    await create_audit_log(
        session=session,
        action="archive",
        resource_type="category",
        resource_id=row.id,
        user_id=current_user.id,
        old_values=old_values,
        new_values=row.model_dump(),
        details={"message": f"User {current_user.username} archived category '{row.name}'"},
        **client_info(request, auth),
    )
    await session.commit()
    await session.refresh(row)
    return CategoryResponse.model_validate(row)


@router.post(
    CategoryRoutes.CATEGORY_RESTORE.value,
    response_model=CategoryResponse,
    responses=problems(ErrorCode.CATEGORY_NOT_FOUND, ErrorCode.CATEGORY_NOT_ARCHIVED, *ADMIN),
)
async def restore_category(
        request: Request,
        category_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.CATEGORY_VIEW, Permissions.CATEGORY_ARCHIVE], admin=True))
):
    """Restore an archived category (is_deleted=false). Not archived: 409."""
    current_user = auth.user
    result = await session.exec(select(Category).where(Category.id == category_id))
    row = result.first()
    if not row:
        raise ApiError(ErrorCode.CATEGORY_NOT_FOUND)
    if not row.is_deleted:
        raise ApiError(ErrorCode.CATEGORY_NOT_ARCHIVED)

    old_values = row.model_dump()
    row.is_deleted = False
    session.add(row)
    await create_audit_log(
        session=session,
        action="restore",
        resource_type="category",
        resource_id=row.id,
        user_id=current_user.id,
        old_values=old_values,
        new_values=row.model_dump(),
        details={"message": f"User {current_user.username} restored category '{row.name}'"},
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
        skip: int = 0,
        limit: int = 100,
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

    result = await session.exec(query.offset(skip).limit(limit))
    signal_types = result.all()

    return [SignalTypeResponse.model_validate(st) for st in signal_types]
