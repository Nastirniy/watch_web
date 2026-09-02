from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select

from app.backend.database import get_session
from app.backend.models.post import Post
from app.backend.models.user import User
from app.backend.services.auth_context import AuthContext
from app.backend.routes.models.response.error_response import ACCESS_ERROR_RESPONSES
from app.backend.routes.models.request.posts_request import PostCreate, PostUpdate
from app.backend.services.access_control import AccessControlService
from app.backend.services.permissions import require
from app.backend.enums.general import Permissions
from app.backend.enums.routes import PostRoutes, SystemRoutes
from app.backend.helpers.audit_helper import create_audit_log
from app.backend.filters.params.post_params import PostFilterParams
from app.backend.filters.post_filter import PostFilterService
from app.backend.routes.models.response.search_response import EntitySchemaInfo
from app.backend.routes.models.response.posts_response import PostResponse, post_to_response

router = APIRouter(responses=ACCESS_ERROR_RESPONSES)


@router.get(SystemRoutes.ROOT.value, response_model=List[PostResponse])
async def list_posts(
        filters: PostFilterParams = Depends(),
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.POST_VIEW]))
):
    """List posts with filters

    Note:
        - Admin users can see all posts
        - Regular users only see their assigned post
    """
    current_user = auth.user
    query = select(Post)
    query = PostFilterService.apply_filters(query, filters, AccessControlService.post_scope(auth))
    result = await session.exec(query)
    return [post_to_response(post) for post in result.all()]


@router.post(SystemRoutes.ROOT.value, response_model=PostResponse)
async def create_post(
        request: Request,
        post_data: PostCreate,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.POST_VIEW, Permissions.POST_CREATE], admin=True))
):
    """Create new post"""
    current_user = auth.user
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

    return post_to_response(post)


@router.get(PostRoutes.FILTER_SCHEMA.value, response_model=EntitySchemaInfo)
async def get_post_filter_schema(
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.POST_VIEW]))
):
    """Available filter params and sortable fields for posts."""
    return EntitySchemaInfo(**PostFilterService.get_schema(is_admin=auth.is_admin))


@router.get(PostRoutes.POST_BY_ID.value, response_model=PostResponse)
async def get_post(
        post_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.POST_VIEW]))
):
    """Get post by ID

    Note:
        - Admin users can view any post
        - Regular users can only view their assigned post
    """
    current_user = auth.user
    AccessControlService.check_post_view(auth, post_id, "post")

    result = await session.exec(
        select(Post).where(Post.id == post_id)
    )

    post = result.first()

    if not post:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Post not found"
        )

    return post_to_response(post)


@router.put(PostRoutes.POST_BY_ID.value, response_model=PostResponse)
async def update_post(
        request: Request,
        post_id: int,
        post_data: PostUpdate,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.POST_VIEW, Permissions.POST_UPDATE], admin=True))
):
    """Update post by ID

    Note:
        - Admin users can update any post
        - Regular users can only update their assigned post
    """
    current_user = auth.user
    result = await session.exec(
        select(Post).where(Post.id == post_id)
    )
    post = result.first()

    if not post:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Post not found"
        )

    # Check code uniqueness if being updated
    update_data = post_data.model_dump(exclude_unset=True)
    if "code" in update_data:
        existing = await session.exec(
            select(Post).where(Post.code == update_data["code"], Post.id != post_id)
        )
        if existing.first():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Post with this code already exists"
            )

    # Store old values for auditing
    old_values = post.model_dump()

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

    return post_to_response(post)

