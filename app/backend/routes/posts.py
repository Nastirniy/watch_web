from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
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

router = APIRouter()


@router.get(SystemRoutes.ROOT.value, response_model=List[Post])
@PermissionService.require_permissions({"all": [
    Permissions.POST_VIEW.value,
    Permissions.IS_ADMIN.value]})
async def list_posts(
        skip: int = 0,
        limit: int = 100,
        is_active: Optional[bool] = None,
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
    """List posts

    Args:
        is_active: True = active posts only, False = deleted posts only, None = all posts (default)

    Note:
        - Admin users can see all posts
        - Regular users only see their assigned post
    """
    query = select(Post)

    if is_active is not None:
        query = query.where(Post.is_active == is_active)

    result = await session.exec(query.offset(skip).limit(limit))
    return result.all()


@router.post(SystemRoutes.ROOT.value, response_model=Post)
@PermissionService.require_permissions({"all": [
    Permissions.POST_CREATE.value,
    Permissions.IS_ADMIN.value]})
async def create_post(
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

    return post


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

    update_data = post_data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(post, key, value)

    session.add(post)
    await session.commit()
    await session.refresh(post)

    return post

