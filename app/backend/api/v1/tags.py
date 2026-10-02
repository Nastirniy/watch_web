from typing import List, Annotated
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, Request, Query
from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.database import get_session
from app.backend.services.access_control import AccessControlService
from app.backend.models.observation import Tag
from app.backend.services.auth_context import AuthContext
from app.backend.services.permissions import require
from app.backend.enums.general import Permissions
from app.backend.enums.routes import TagRoutes
from app.backend.helpers.audit_helper import client_info, create_audit_log
from app.backend.api.shared.models.request.tag_request import TagCreate, TagUpdate
from app.backend.api.shared.models.response.tag_response import TagResponse
from app.backend.filters.params.tag_params import TagFilterParams
from app.backend.filters.tag_filter import TagFilterService
from app.backend.services.api_errors import ApiError
from app.backend.enums.errors import AccessErrorCode, ErrorCode
from app.backend.openapi_problems import ADMIN, GUARDED, problems

router = APIRouter(prefix=TagRoutes.BASE.value, tags=[TagRoutes.tag()])


@router.get(
    TagRoutes.LIST_ALL.value,
    response_model=List[TagResponse],
    responses=problems(AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION, *GUARDED),
)
async def list_tags(
        filters: Annotated[TagFilterParams, Query()],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.TAG_VIEW]))
):
    AccessControlService.check_include_deleted(auth, filters, Permissions.TAG_ARCHIVE)
    query = select(Tag)
    query = TagFilterService.apply_filters(query, filters)
    result = await session.exec(query)
    tags = result.all()

    return [TagResponse.model_validate(t) for t in tags]


@router.post(
    TagRoutes.CREATE.value,
    response_model=TagResponse,
    responses=problems(ErrorCode.TAG_NAME_TAKEN, *ADMIN),
)
async def create_tag(
        request: Request,
        tag_data: TagCreate,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.TAG_VIEW, Permissions.TAG_CREATE], admin=True))
):
    current_user = auth.user
    existing_result = await session.exec(
        select(Tag).where(
            func.lower(Tag.name) == func.lower(tag_data.name)
        )
    )
    existing_tag = existing_result.first()

    if existing_tag:
        if not existing_tag.is_deleted:
            raise ApiError(ErrorCode.TAG_NAME_TAKEN, name=tag_data.name)
        
        # Restore archived tag
        old_values = existing_tag.model_dump()
        existing_tag.is_deleted = False
        existing_tag.created_at = datetime.now(timezone.utc)
        tag = existing_tag
        action = "restore"
    else:
        tag = Tag(name=tag_data.name)
        old_values = None
        action = "create"

    session.add(tag)

    await session.commit()
    await session.refresh(tag)

    # Create audit log
    await create_audit_log(
        session=session,
        action=action,
        resource_type="tag",
        resource_id=tag.id,
        user_id=current_user.id,
        old_values=old_values,
        new_values=tag.model_dump(),
        details={"message": f"User {current_user.username} {action}d tag '{tag.name}'"},
        **client_info(request, auth)
    )
    await session.commit()

    return TagResponse.model_validate(tag)


@router.get(
    TagRoutes.TAG_BY_ID.value,
    response_model=TagResponse,
    responses=problems(ErrorCode.TAG_NOT_FOUND, *GUARDED),
)
async def get_tag(
        tag_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.TAG_VIEW]))
):
    result = await session.exec(
        select(Tag).where(
            Tag.id == tag_id,
            Tag.is_deleted == False
        )
    )
    tag = result.first()

    if not tag:
        raise ApiError(ErrorCode.TAG_NOT_FOUND)

    return TagResponse.model_validate(tag)


@router.put(
    TagRoutes.TAG_BY_ID.value,
    response_model=TagResponse,
    responses=problems(
        ErrorCode.TAG_NAME_ARCHIVED, ErrorCode.TAG_NAME_TAKEN, ErrorCode.TAG_NOT_FOUND,
        *ADMIN,
    ),
)
async def update_tag(
        request: Request,
        tag_id: int,
        tag_data: TagUpdate,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.TAG_VIEW, Permissions.TAG_UPDATE], admin=True))
):
    current_user = auth.user
    result = await session.exec(
        select(Tag).where(
            Tag.id == tag_id,
            Tag.is_deleted == False
        )
    )
    tag = result.first()

    if not tag:
        raise ApiError(ErrorCode.TAG_NOT_FOUND)

    # Store old values for auditing
    old_values = tag.model_dump()

    if tag_data.name is not None:
        existing_result = await session.exec(
            select(Tag).where(
                func.lower(Tag.name) == func.lower(tag_data.name),
                Tag.id != tag_id
            )
        )
        existing_tag = existing_result.first()

        if existing_tag:
            code = ErrorCode.TAG_NAME_ARCHIVED if existing_tag.is_deleted else ErrorCode.TAG_NAME_TAKEN
            raise ApiError(code, name=tag_data.name)

    update_data = tag_data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(tag, field, value)

    session.add(tag)

    await session.commit()
    await session.refresh(tag)

    # Create audit log
    await create_audit_log(
        session=session,
        action="update",
        resource_type="tag",
        resource_id=tag.id,
        user_id=current_user.id,
        old_values=old_values,
        new_values=tag.model_dump(),
        details={"message": f"User {current_user.username} updated tag '{tag.name}'"},
        **client_info(request, auth)
    )
    await session.commit()

    return TagResponse.model_validate(tag)


@router.post(
    TagRoutes.TAG_ARCHIVE.value,
    response_model=TagResponse,
    responses=problems(ErrorCode.TAG_NOT_FOUND, *ADMIN),
)
async def archive_tag(
        request: Request,
        tag_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.TAG_VIEW, Permissions.TAG_ARCHIVE], admin=True))
):
    """Archive a tag (is_deleted=true). Nothing is removed: the row is hidden from lists unless
    include_deleted=true and cannot be used for new data. Undo with POST /{tag_id}/restore. Already archived or unknown: 404."""
    current_user = auth.user
    result = await session.exec(select(Tag).where(Tag.id == tag_id, Tag.is_deleted == False))
    row = result.first()
    if not row:
        raise ApiError(ErrorCode.TAG_NOT_FOUND)

    old_values = row.model_dump()
    row.is_deleted = True
    session.add(row)
    await create_audit_log(
        session=session,
        action="archive",
        resource_type="tag",
        resource_id=row.id,
        user_id=current_user.id,
        old_values=old_values,
        new_values=row.model_dump(),
        details={"message": f"User {current_user.username} archived tag '{row.name}'"},
        **client_info(request, auth),
    )
    await session.commit()
    await session.refresh(row)
    return TagResponse.model_validate(row)


@router.post(
    TagRoutes.TAG_RESTORE.value,
    response_model=TagResponse,
    responses=problems(ErrorCode.TAG_NOT_FOUND, ErrorCode.TAG_NOT_ARCHIVED, *ADMIN),
)
async def restore_tag(
        request: Request,
        tag_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.TAG_VIEW, Permissions.TAG_ARCHIVE], admin=True))
):
    """Restore an archived tag (is_deleted=false). Not archived: 409."""
    current_user = auth.user
    result = await session.exec(select(Tag).where(Tag.id == tag_id))
    row = result.first()
    if not row:
        raise ApiError(ErrorCode.TAG_NOT_FOUND)
    if not row.is_deleted:
        raise ApiError(ErrorCode.TAG_NOT_ARCHIVED)

    old_values = row.model_dump()
    row.is_deleted = False
    session.add(row)
    await create_audit_log(
        session=session,
        action="restore",
        resource_type="tag",
        resource_id=row.id,
        user_id=current_user.id,
        old_values=old_values,
        new_values=row.model_dump(),
        details={"message": f"User {current_user.username} restored tag '{row.name}'"},
        **client_info(request, auth),
    )
    await session.commit()
    await session.refresh(row)
    return TagResponse.model_validate(row)
