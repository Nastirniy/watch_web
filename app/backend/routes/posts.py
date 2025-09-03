from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from pydantic import BaseModel
from datetime import datetime

from app.backend.database import get_session
from app.backend.models.post import Post
from app.backend.models.user import User
from app.backend.dependencies import get_current_user
from app.backend.services.permissions import PermissionService
from app.backend.enums import Routes, Permissions, ConnectionStatus

router = APIRouter()


class PostCreate(BaseModel):
    name: str
    code: str
    description: Optional[str] = None
    region: Optional[str] = None


class PostUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    region: Optional[str] = None
    is_active: Optional[bool] = None


@router.get(Routes.POSTS_LIST.value.replace("/posts", ""), response_model=List[Post])
@PermissionService.require_permission(Permissions.POST_VIEW.value)
async def list_posts(
        skip: int = 0,
        limit: int = 100,
        is_active: Optional[bool] = None,
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
    """List all posts"""
    query = select(Post)

    if is_active is not None:
        query = query.where(Post.is_active == is_active)

    result = await session.exec(query.offset(skip).limit(limit))
    return result.all()


@router.post(Routes.POSTS_CREATE.value.replace("/posts", ""), response_model=Post)
@PermissionService.require_permission(Permissions.POST_CREATE.value)
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

    post = Post(**post_data.dict())
    session.add(post)
    await session.commit()
    await session.refresh(post)

    return post


@router.get(Routes.POSTS_GET.value.replace("/posts/", ""), response_model=Post)
@PermissionService.require_permission(Permissions.POST_VIEW.value)
async def get_post(
        post_id: int,
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
    """Get post by ID"""
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