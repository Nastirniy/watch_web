from typing import List, Optional, Annotated
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, Query, Request
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app.backend.database import get_session
from app.backend.models.post import Post
from app.backend.services.auth_context import AuthContext
from app.backend.api.shared.models.request.posts_request import PostCreate, PostUpdate
from app.backend.services.access_control import AccessControlService
from app.backend.services.permissions import require
from app.backend.enums.general import Permissions, StatsGroupBy
from app.backend.config import settings
from app.backend.enums.routes import PostRoutes
from app.backend.helpers.audit_helper import client_info, create_audit_log
from app.backend.filters.params.post_params import PostFilterParams
from app.backend.filters.post_filter import PostFilterService
from app.backend.api.shared.models.response.posts_response import PostResponse, PostObservationStatsResponse, post_to_response
from app.backend.services.observation_counts import post_observation_stats
from app.backend.services.api_errors import ApiError
from app.backend.enums.errors import AccessErrorCode, ErrorCode
from app.backend.openapi_problems import ADMIN, GUARDED, problems

router = APIRouter(prefix=PostRoutes.BASE.value, tags=[PostRoutes.tag()])


def _code_taken(code: str, exclude_post_id: Optional[int] = None):
    """Case-insensitive lookup, like the unique index under the default MySQL collation"""
    query = select(Post).where(func.lower(Post.code) == code.lower())
    if exclude_post_id is not None:
        query = query.where(Post.id != exclude_post_id)
    return query


async def _commit_post(session: AsyncSession) -> None:
    """Commit a post write; a code taken by a concurrent request after the check hits the unique index"""
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise ApiError(ErrorCode.POST_CODE_TAKEN)


@router.get(PostRoutes.ROOT.value, response_model=List[PostResponse], responses=problems(*GUARDED))
async def list_posts(
        filters: Annotated[PostFilterParams, Query()],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.POST_VIEW]))
):
    """List posts with filters

    Note:
        - Administrators (admin.true) see every post record
        - Everyone else sees the posts of their data scope (home post, user_post_access, or all with post.access_all)
    """
    query = select(Post)
    query = PostFilterService.apply_filters(query, filters, AccessControlService.post_record_scope(auth))
    result = await session.exec(query)
    return [post_to_response(post) for post in result.all()]


@router.post(PostRoutes.ROOT.value, response_model=PostResponse, responses=problems(ErrorCode.POST_CODE_TAKEN, *ADMIN))
async def create_post(
        request: Request,
        post_data: PostCreate,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.POST_VIEW, Permissions.POST_CREATE], admin=True))
):
    """Create new post"""
    current_user = auth.user
    # Check if post with code exists
    result = await session.exec(_code_taken(post_data.code))
    if result.first():
        raise ApiError(ErrorCode.POST_CODE_TAKEN)

    post = Post(**post_data.model_dump())
    session.add(post)
    await _commit_post(session)
    await session.refresh(post)

    # Create audit log
    await create_audit_log(
        session=session,
        action="create",
        resource_type="post",
        resource_id=post.id,
        user_id=current_user.id,
        new_values=post.model_dump(),
        details={"message": f"User {current_user.username} created new post '{post.name}' ({post.code})"},
        **client_info(request, auth)
    )
    await session.commit()

    return post_to_response(post)


@router.get(
    PostRoutes.POST_BY_ID.value,
    response_model=PostResponse,
    responses=problems(
        AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.POST_ACCESS_DENIED, ErrorCode.POST_NOT_FOUND, *GUARDED,
    ),
)
async def get_post(
        post_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.POST_VIEW]))
):
    """Get post by ID

    Note:
        - Administrators (admin.true) can view any post record
        - Everyone else needs the post in their data scope
    """
    AccessControlService.check_post_record_view(auth, post_id)

    result = await session.exec(
        select(Post).where(Post.id == post_id)
    )

    post = result.first()

    if not post:
        raise ApiError(ErrorCode.POST_NOT_FOUND)

    return post_to_response(post)


@router.get(
    PostRoutes.POST_OBSERVATION_STATS.value,
    response_model=PostObservationStatsResponse,
    responses=problems(
        ErrorCode.PERIOD_REVERSED, ErrorCode.PERIOD_TOO_LONG, ErrorCode.UNKNOWN_TIME_ZONE,
        AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.POST_ACCESS_DENIED, ErrorCode.POST_NOT_FOUND, *GUARDED,
    ),
)
async def get_post_observation_stats(
        post_id: int,
        date_from: Optional[datetime] = Query(None, description="Period start, created_at (default: date_to minus 30 days; naive values are UTC)"),
        date_to: Optional[datetime] = Query(None, description="Period end, created_at (default: now)"),
        group_by: StatsGroupBy = Query(StatsGroupBy.DAY, description="Bucket size: hour (period up to 31 days) or day (up to 366 days)"),
        tz: Optional[str] = Query(None, description="IANA time zone of the buckets (default: server time zone setting)"),
        include_archived: bool = Query(False, description="Count archived observations separately; counts only, so observation.view is enough"),
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.POST_VIEW, Permissions.OBSERVATION_VIEW]))
):
    """Observation counters of a post by created_at: all time, last 24 hours, and the period
    per hour or per day with every bucket included. not_archived and archived never overlap.

    Note:
        - Administrators (admin.true) count any post, like GET /statistics
        - Everyone else needs the post in their data scope
    """
    AccessControlService.check_post_record_view(auth, post_id)
    if (await session.exec(select(Post.id).where(Post.id == post_id))).first() is None:
        raise ApiError(ErrorCode.POST_NOT_FOUND)
    return await post_observation_stats(
        session, post_id, date_from, date_to, group_by, tz or settings.timezone.timezone, include_archived,
    )


@router.put(
    PostRoutes.POST_BY_ID.value,
    response_model=PostResponse,
    responses=problems(ErrorCode.POST_CODE_TAKEN, ErrorCode.POST_NOT_FOUND, *ADMIN),
)
async def update_post(
        request: Request,
        post_id: int,
        post_data: PostUpdate,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.POST_VIEW, Permissions.POST_UPDATE], admin=True))
):
    """Update post by ID. Administration route: any post, only for administrators.

    Only the fields sent are changed; null is refused for name, code and is_active (omit them instead).
    """
    current_user = auth.user
    result = await session.exec(
        select(Post).where(Post.id == post_id)
    )
    post = result.first()

    if not post:
        raise ApiError(ErrorCode.POST_NOT_FOUND)

    # Check code uniqueness if being updated
    update_data = post_data.model_dump(exclude_unset=True)
    if "code" in update_data:
        existing = await session.exec(_code_taken(update_data["code"], exclude_post_id=post_id))
        if existing.first():
            raise ApiError(ErrorCode.POST_CODE_TAKEN)

    # Store old values for auditing
    old_values = post.model_dump()

    for key, value in update_data.items():
        setattr(post, key, value)
    post.updated_at = datetime.now(timezone.utc)

    session.add(post)
    await _commit_post(session)
    await session.refresh(post)

    # Create audit log
    await create_audit_log(
        session=session,
        action="update",
        resource_type="post",
        resource_id=post.id,
        user_id=current_user.id,
        old_values=old_values,
        new_values=post.model_dump(),
        details={"message": f"User {current_user.username} updated post '{post.name}'"},
        **client_info(request, auth)
    )
    await session.commit()

    return post_to_response(post)

