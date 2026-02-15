from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select

from app.backend.database import get_session
from app.backend.models.post import Post
from app.backend.models.user import User
from app.backend.dependencies import get_current_user
from app.backend.routes.models.request.posts_request import PostCreate, PostUpdate
from app.backend.services.access_control import AccessControlService
from app.backend.services.permissions import PermissionService
from app.backend.enums.general import Permissions
from app.backend.enums.routes import PostRoutes, SystemRoutes
from app.backend.helpers.audit_helper import create_audit_log
from app.backend.filters.params.post_params import PostFilterParams
from app.backend.filters.post_filter import PostFilterService
from app.backend.routes.models.response.search_response import EntitySchemaInfo

router = APIRouter()


@router.get(SystemRoutes.ROOT.value, response_model=List[Post])
@PermissionService.require_permissions({"all": [
    Permissions.POST_VIEW.value,
    Permissions.IS_ADMIN.value]})
async def list_posts(
        filters: PostFilterParams = Depends(),
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
    """List posts with filters

    Note:
        - Admin users can see all posts
        - Regular users only see their assigned post
    """
    admin = await PermissionService.has_permission(
        session, current_user, Permissions.IS_ADMIN.value
    )
    access_post_id = None if admin else current_user.post_id

    query = select(Post)
    query = PostFilterService.apply_filters(query, filters, access_post_id)
    result = await session.exec(query)
    return result.all()


@router.post(SystemRoutes.ROOT.value, response_model=Post)
@PermissionService.require_permissions({"all": [
    Permissions.POST_CREATE.value,
    Permissions.IS_ADMIN.value]})
async def create_post(
        request: Request,
        post_data: PostCreate,
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
    """Create new post"""
    # Check if post with code exists
    result = await session.exec(
        select(Post).where(Post.code == post_data.code)
    )
    if result.first():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Post with this code already exists"
        )

    post = Post(**post_data.model_dump())
    session.add(post)
    await session.commit()
    await session.refresh(post)

    # Create audit log
    await create_audit_log(
        session=session,
        action="create",
        resource_type="post",
        resource_id=post.id,
        user_id=current_user.id,
        session_id=getattr(request.state, "session_id", None),
        new_values=post.model_dump(),
        details={"message": f"User {current_user.username} created new post '{post.name}' ({post.code})"},
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        request_url=str(request.url)
    )
    await session.commit()

    return post


@router.get(PostRoutes.FILTER_SCHEMA.value, response_model=EntitySchemaInfo)
@PermissionService.require_permissions({"all": [
    Permissions.POST_VIEW.value,
    Permissions.IS_ADMIN.value]})
async def get_post_filter_schema(
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
    """Available filter params and sortable fields for posts."""
    return EntitySchemaInfo(**PostFilterService.get_schema(is_admin=True))


@router.get(PostRoutes.POST_BY_ID.value, response_model=Post)
@PermissionService.require_permissions({"any": [
    Permissions.POST_VIEW.value,
    Permissions.IS_ADMIN.value]})
async def get_post(
        post_id: int,
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
    """Get post by ID

    Note:
        - Admin users can view any post
        - Regular users can only view their assigned post
    """
    await AccessControlService.check_post_membership(
        current_user, post_id, session, "view"
    )

    result = await session.exec(
        select(Post).where(Post.id == post_id)
    )

    post = result.first()

    if not post:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Post not found"
        )

    return post


@router.put(PostRoutes.POST_BY_ID.value, response_model=Post)
@PermissionService.require_permissions({"all": [
    Permissions.POST_UPDATE.value,
    Permissions.POST_VIEW.value,
    Permissions.IS_ADMIN.value]})
async def update_post(
        request: Request,
        post_id: int,
        post_data: PostUpdate,
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
    """Update post by ID

    Note:
        - Admin users can update any post
        - Regular users can only update their assigned post
    """
    result = await session.exec(
        select(Post).where(Post.id == post_id)
    )
    post = result.first()

    if not post:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Post not found"
        )

    # Store old values for auditing
    old_values = post.model_dump()

    update_data = post_data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(post, key, value)

    session.add(post)
    await session.commit()
    await session.refresh(post)

    # Create audit log
    await create_audit_log(
        session=session,
        action="update",
        resource_type="post",
        resource_id=post.id,
        user_id=current_user.id,
        session_id=getattr(request.state, "session_id", None),
        old_values=old_values,
        new_values=post.model_dump(),
        details={"message": f"User {current_user.username} updated post '{post.name}'"},
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        request_url=request.url.path if hasattr(request, "url") else None
    )
    await session.commit()

    return post

