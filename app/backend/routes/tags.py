from typing import List
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, status as fapi_status, Request
from loguru import logger
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.database import get_session
from app.backend.models.observation import Tag
from app.backend.models.user import User
from app.backend.dependencies import get_current_user
from app.backend.services.permissions import PermissionService
from app.backend.enums.general import Permissions
from app.backend.enums.routes import TagRoutes
from app.backend.helpers.audit_helper import create_audit_log
from app.backend.routes.models.request.tag_request import TagCreate, TagUpdate
from app.backend.routes.models.response.tag_response import TagResponse
from app.backend.routes.models.response.observations_response import MessageResponse
from app.backend.filters.params.tag_params import TagFilterParams
from app.backend.filters.tag_filter import TagFilterService
from app.backend.routes.models.response.search_response import EntitySchemaInfo

router = APIRouter()


@router.get(TagRoutes.LIST_ALL.value, response_model=List[TagResponse])
@PermissionService.require_permissions({"all": [
    Permissions.TAG_VIEW.value,
    Permissions.IS_ADMIN.value]})
async def list_tags(
        filters: TagFilterParams = Depends(),
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
    query = select(Tag)
    query = TagFilterService.apply_filters(query, filters)
    result = await session.exec(query)
    tags = result.all()

    return [TagResponse.model_validate(t) for t in tags]


@router.get(TagRoutes.FILTER_SCHEMA.value, response_model=EntitySchemaInfo)
@PermissionService.require_permissions({"all": [
    Permissions.TAG_VIEW.value,
    Permissions.IS_ADMIN.value]})
async def get_tag_filter_schema(
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
    """Available filter params and sortable fields for tags."""
    return EntitySchemaInfo(**TagFilterService.get_schema(is_admin=True))


@router.post(TagRoutes.CREATE.value, response_model=TagResponse)
@PermissionService.require_permissions({"all": [
    Permissions.TAG_CREATE.value,
    Permissions.IS_ADMIN.value]})
async def create_tag(
        request: Request,
        tag_data: TagCreate,
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
    existing_result = await session.exec(
        select(Tag).where(
            func.lower(Tag.name) == func.lower(tag_data.name)
        )
    )
    existing_tag = existing_result.first()

    if existing_tag:
        if not existing_tag.is_deleted:
            raise HTTPException(
                status_code=fapi_status.HTTP_400_BAD_REQUEST,
                detail=f"Tag with name '{tag_data.name}' already exists"
            )
        
        # Restore soft-deleted tag
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

    try:
        await session.commit()
        await session.refresh(tag)

        # Create audit log
        await create_audit_log(
            session=session,
            action=action,
            resource_type="tag",
            resource_id=tag.id,
            user_id=current_user.id,
            session_id=getattr(request.state, "session_id", None),
            old_values=old_values,
            new_values=tag.model_dump(),
            details={"message": f"User {current_user.username} {action}d tag '{tag.name}'"},
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_url=str(request.url)
        )
        await session.commit()

        return TagResponse.model_validate(tag)
    except IntegrityError as e:
        await session.rollback()
        logger.exception(f"Unexpected database constraint violation: {str(e.orig) if hasattr(e, 'orig') else str(e)}")
        raise HTTPException(
            status_code=fapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unexpected database constraint violation"
        )


@router.get(TagRoutes.TAG_BY_ID.value, response_model=TagResponse)
@PermissionService.require_permissions({"all": [
    Permissions.TAG_VIEW.value,
    Permissions.IS_ADMIN.value]})
async def get_tag(
        tag_id: int,
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
    result = await session.exec(
        select(Tag).where(
            Tag.id == tag_id,
            Tag.is_deleted == False
        )
    )
    tag = result.first()

    if not tag:
        raise HTTPException(
            status_code=fapi_status.HTTP_404_NOT_FOUND,
            detail="Tag not found"
        )

    return TagResponse.model_validate(tag)


@router.put(TagRoutes.TAG_BY_ID.value, response_model=TagResponse)
@PermissionService.require_permissions({"all": [
    Permissions.TAG_UPDATE.value,
    Permissions.IS_ADMIN.value]})
async def update_tag(
        request: Request,
        tag_id: int,
        tag_data: TagUpdate,
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
    result = await session.exec(
        select(Tag).where(
            Tag.id == tag_id,
            Tag.is_deleted == False
        )
    )
    tag = result.first()

    if not tag:
        raise HTTPException(
            status_code=fapi_status.HTTP_404_NOT_FOUND,
            detail="Tag not found"
        )

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
            status_msg = "already exists" if not existing_tag.is_deleted else "already exists as a deleted tag"
            raise HTTPException(
                status_code=fapi_status.HTTP_400_BAD_REQUEST,
                detail=f"Tag with name '{tag_data.name}' {status_msg}"
            )

    update_data = tag_data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(tag, field, value)

    session.add(tag)

    try:
        await session.commit()
        await session.refresh(tag)

        # Create audit log
        await create_audit_log(
            session=session,
            action="update",
            resource_type="tag",
            resource_id=tag.id,
            user_id=current_user.id,
            session_id=getattr(request.state, "session_id", None),
            old_values=old_values,
            new_values=tag.model_dump(),
            details={"message": f"User {current_user.username} updated tag '{tag.name}'"},
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_url=str(request.url)
        )
        await session.commit()

        return TagResponse.model_validate(tag)
    except IntegrityError as e:
        await session.rollback()
        logger.exception(f"Unexpected database constraint violation: {str(e.orig) if hasattr(e, 'orig') else str(e)}")
        raise HTTPException(
            status_code=fapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Unexpected database constraint violation"
        )


@router.delete(TagRoutes.TAG_BY_ID.value, response_model=MessageResponse)
@PermissionService.require_permissions({"all": [
    Permissions.TAG_DELETE.value,
    Permissions.IS_ADMIN.value]})
async def delete_tag(
        request: Request,
        tag_id: int,
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
    result = await session.exec(
        select(Tag).where(
            Tag.id == tag_id,
            Tag.is_deleted == False
        )
    )
    tag = result.first()

    if not tag:
        raise HTTPException(
            status_code=fapi_status.HTTP_404_NOT_FOUND,
            detail="Tag not found"
        )

    # Store old values for auditing
    old_values = tag.model_dump()

    tag.is_deleted = True
    session.add(tag)
    
    # Create audit log
    await create_audit_log(
        session=session,
        action="delete",
        resource_type="tag",
        resource_id=tag.id,
        user_id=current_user.id,
        session_id=getattr(request.state, "session_id", None),
        old_values=old_values,
        new_values=tag.model_dump(),
        details={"message": f"User {current_user.username} deleted tag '{tag.name}'"},
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        request_url=str(request.url)
    )
    
    await session.commit()

    return MessageResponse(message="Tag deleted successfully")
