from typing import List, Optional, Annotated
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, Query, Request
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app.backend.api.shared.helpers.db_errors import is_unique_violation
from app.backend.database import get_session
from app.backend.models.post import Post
from app.backend.auth.context import AuthContext
from app.backend.api.v1.models.request.posts import PostCreate, PostUpdate
from app.backend.auth.access_control import AccessControlService
from app.backend.auth.require import require
from app.backend.enums.general import Permissions, StatsGroupBy
from app.backend.config import settings
from app.backend.enums.routes import PostRoutes
from app.backend.api.shared.helpers.audit_helper import client_info
from app.backend.services.audit import create_audit_log
from app.backend.api.v1.models.query.posts import PostFilterParams
from app.backend.filters.post_filter import PostFilterService
from app.backend.api.v1.models.response.posts import PostResponse, PostObservationStatsResponse, post_to_response
from app.backend.services.observation_counts import post_observation_stats
from app.backend.errors.problems import ApiError
from app.backend.errors.coded import CodedError
from app.backend.enums.errors import AccessErrorCode, ErrorCode
from app.backend.errors.openapi import ADMIN, GUARDED, problems
from app.backend.utils.validators import UtcDatetime

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
    except IntegrityError as e:
        await session.rollback()
        if is_unique_violation(e):  # code is the only unique index of posts
            raise ApiError(ErrorCode.POST_CODE_TAKEN)
        raise


@router.get(
    PostRoutes.ROOT.value, response_model=List[PostResponse],
    responses=problems(AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION, *GUARDED),
)
async def list_posts(
        filters: Annotated[PostFilterParams, Query()],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.POST_VIEW]))
):
    """List posts with filters

    Note:
        - Administrators (admin.true) see every post record
        - Everyone else sees the posts of their data scope (home post, user_post_access, or all with post.access_all);
          an archived post leaves the home-post and access-row scope
        - Archived posts only with include_deleted (requires post.archive)
    """
    AccessControlService.check_include_deleted(auth, filters, Permissions.POST_ARCHIVE)
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
        - An archived post answers 404 unless the caller holds post.archive or is an administrator
    """
    AccessControlService.check_post_record_view(auth, post_id)

    result = await session.exec(
        select(Post).where(Post.id == post_id)
    )

    post = result.first()

    if not post:
        raise ApiError(ErrorCode.POST_NOT_FOUND)
    # The state after the scope: archived posts are for those who manage the archive (and administrators, whose
    # post.view is the route's)
    if post.is_deleted and not (auth.is_admin or auth.has(Permissions.POST_ARCHIVE)):
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
        date_from: Optional[UtcDatetime] = Query(None, description="Period start, created_at (default: date_to minus 30 days; naive values are UTC)"),
        date_to: Optional[UtcDatetime] = Query(None, description="Period end, created_at (default: now)"),
        group_by: StatsGroupBy = Query(StatsGroupBy.DAY, description="Bucket size: hour (period up to 31 days) or day (up to 366 days)"),
        tz: Optional[str] = Query(None, max_length=64, description="IANA time zone of the buckets (default: server time zone setting)"),
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
    try:
        stats = await post_observation_stats(
            session, post_id, date_from, date_to, group_by, tz or settings.timezone.timezone, include_archived,
        )
    except CodedError as exc:
        raise ApiError.from_coded(exc)
    return PostObservationStatsResponse.model_validate(stats, from_attributes=True)


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
    Archived posts answer 404 until restored (POST /{post_id}/restore); is_deleted is never read from the body.
    """
    current_user = auth.user
    result = await session.exec(
        select(Post).where(Post.id == post_id)
    )
    post = result.first()

    if not post or post.is_deleted:
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


async def _set_post_archived(
        request: Request,
        session: AsyncSession,
        auth: AuthContext,
        post_id: int,
        archived: bool,
) -> PostResponse:
    """Shared body of archive / restore: state checks, flag flip, audit row (administration: any post)"""
    current_user = auth.user
    post = (await session.exec(select(Post).where(Post.id == post_id))).first()
    if not post or (archived and post.is_deleted):
        raise ApiError(ErrorCode.POST_NOT_FOUND)
    if not archived and not post.is_deleted:
        raise ApiError(ErrorCode.POST_NOT_ARCHIVED)

    action = "archive" if archived else "restore"
    old_values = post.model_dump()
    post.is_deleted = archived
    post.updated_at = datetime.now(timezone.utc)
    session.add(post)
    await create_audit_log(
        session=session,
        action=action,
        resource_type="post",
        resource_id=post.id,
        user_id=current_user.id,
        old_values=old_values,
        new_values=post.model_dump(),
        details={"message": f"User {current_user.username} {action}d post '{post.name}' ({post.code})"},
        **client_info(request, auth)
    )
    await session.commit()
    await session.refresh(post)
    return post_to_response(post)


@router.post(
    PostRoutes.POST_ARCHIVE.value,
    response_model=PostResponse,
    responses=problems(ErrorCode.POST_NOT_FOUND, *ADMIN),
)
async def archive_post(
        request: Request,
        post_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.POST_VIEW, Permissions.POST_ARCHIVE], admin=True))
):
    """Archive a post (is_deleted=true). Administration route: any post, only for administrators.

    Nothing is removed and no user row changes. The post leaves the data scope of its home users and of its
    user_post_access rows until it is restored (they keep the rows); only post.access_all still reads its data. Its
    observations and media become read-only history: writes answer 409 post_archived, except that administrators
    (admin.true with observation.view and observation.archive) still archive and restore its observations. It cannot
    become a home post or get new access rows. Hidden from lists unless include_deleted=true.
    Undo with POST /{post_id}/restore. Already archived or unknown: 404.
    """
    return await _set_post_archived(request, session, auth, post_id, archived=True)


@router.post(
    PostRoutes.POST_RESTORE.value,
    response_model=PostResponse,
    responses=problems(ErrorCode.POST_NOT_FOUND, ErrorCode.POST_NOT_ARCHIVED, *ADMIN),
)
async def restore_post(
        request: Request,
        post_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.POST_VIEW, Permissions.POST_ARCHIVE], admin=True))
):
    """Restore an archived post (is_deleted=false): its home users and access rows read and write its data again.
    Unknown: 404. Not archived: 409."""
    return await _set_post_archived(request, session, auth, post_id, archived=False)
