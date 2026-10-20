from typing import List, Optional, Annotated
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, Query, Request
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select
from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app.backend.api.shared.helpers.db_errors import is_unique_violation
from app.backend.database import get_session
from app.backend.utils.row_locks import lock, lock_first
from app.backend.api.shared.helpers.scope_group_helper import lock_groups
from app.backend.models.post import Post
from app.backend.auth.context import AuthContext
from app.backend.api.v1.models.request.posts import PostCreate, PostUpdate
from app.backend.auth.access_control import AccessControlService
from app.backend.auth.require import require
from app.backend.auth.scope import Node, ROOT
from app.backend.enums.general import Permissions, StatsGroupBy
from app.backend.config import settings
from app.backend.enums.routes import PostRoutes
from app.backend.api.shared.helpers.audit_helper import client_info
from app.backend.services.audit import create_audit_log
from app.backend.api.v1.models.query.posts import PostFilterParams
from app.backend.filters.post_filter import PostFilterService
from app.backend.api.v1.models.response.posts import PostResponse, PostObservationStatsResponse, posts_to_response
from app.backend.services.observation_counts import post_observation_stats
from app.backend.services.scope_groups import get_group
from app.backend.services.units import unit_filter_of
from app.backend.errors.problems import ApiError
from app.backend.errors.coded import CodedError
from app.backend.enums.errors import AccessErrorCode, ErrorCode
from app.backend.errors.openapi import GUARDED, problems
from app.backend.utils.validators import UtcDatetime

router = APIRouter(prefix=PostRoutes.BASE.value, tags=[PostRoutes.tag()])


async def _post_response(session: AsyncSession, auth: AuthContext, post: Post) -> PostResponse:
    return (await posts_to_response(session, auth.tree, [post]))[0]


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
        - The posts where post.view is in effect (assigned at the root, at a group above the post or at the post)
        - Archived posts only with include_deleted (requires post.archive), and only where post.archive is in effect
    """
    archived = AccessControlService.archived_scope(auth, filters, Permissions.POST_ARCHIVE)
    query = select(Post)
    query = PostFilterService.apply_filters(
        query, filters, AccessControlService.scope_for(auth, Permissions.POST_VIEW), archived_post_ids=archived,
        unit_post_ids=unit_filter_of(auth.tree, filters),
    )
    result = await session.exec(query)
    return await posts_to_response(session, auth.tree, list(result.all()))


async def _check_group(session: AsyncSession, group_id: Optional[int]) -> None:
    """The group of a post exists and is not archived (null: directly under the root)"""
    if group_id is None:
        return
    try:
        await get_group(session, group_id)
    except CodedError as exc:
        raise ApiError.from_coded(exc)


def _location(group_id: Optional[int]) -> Node:
    """Where a post sits in the scope tree: its group, or directly under the root"""
    return Node.group(group_id) if group_id is not None else ROOT


@router.post(
    PostRoutes.ROOT.value, response_model=PostResponse,
    responses=problems(
        AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION, ErrorCode.POST_CODE_TAKEN, ErrorCode.SCOPE_GROUP_NOT_FOUND,
        *GUARDED,
    ),
)
async def create_post(
        request: Request,
        post_data: PostCreate,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.POST_VIEW, Permissions.POST_CREATE]))
):
    """Create new post. post.create must be held at the target group (or above it); a post directly under the
    root needs post.create at the root."""
    current_user = auth.user
    AccessControlService.check_covers(auth, Permissions.POST_CREATE, _location(post_data.group_id), "create_post")
    # The group is locked first: an archive of the group waits, so no post appears inside an archived group
    await lock_groups(session, auth.tree, [post_data.group_id])
    await _check_group(session, post_data.group_id)
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

    return await _post_response(session, auth, post)


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
        - post.view must be in effect at the post
        - An archived post answers 404 unless post.archive is in effect at it too
    """
    AccessControlService.check_post(auth, Permissions.POST_VIEW, post_id)

    result = await session.exec(
        select(Post).where(Post.id == post_id)
    )

    post = result.first()

    if not post:
        raise ApiError(ErrorCode.POST_NOT_FOUND)
    # The state after the scope: archived posts are for those who manage the archive at that post
    if post.is_deleted and not auth.has_at_post(Permissions.POST_ARCHIVE, post_id):
        raise ApiError(ErrorCode.POST_NOT_FOUND)

    return await _post_response(session, auth, post)


@router.get(
    PostRoutes.POST_OBSERVATION_STATS.value,
    response_model=PostObservationStatsResponse,
    responses=problems(
        ErrorCode.PERIOD_REVERSED, ErrorCode.PERIOD_TOO_LONG, ErrorCode.STATS_PERIOD_OUT_OF_RANGE,
        ErrorCode.UNKNOWN_TIME_ZONE,
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
        - observation.view must be in effect at the post (counters are observation data)
    """
    AccessControlService.check_post(auth, Permissions.OBSERVATION_VIEW, post_id)
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
    responses=problems(
        AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.POST_WRITE_DENIED,
        AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION, ErrorCode.POST_CODE_TAKEN, ErrorCode.POST_NOT_FOUND,
        ErrorCode.SCOPE_GROUP_NOT_FOUND, *GUARDED,
    ),
)
async def update_post(
        request: Request,
        post_id: int,
        post_data: PostUpdate,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.POST_VIEW, Permissions.POST_UPDATE]))
):
    """Update post by ID. post.update must be in effect at the post; a move (group_id) also needs post.update
    covering both the old and the new location (the group or above it; the root for a post directly under it).

    Only the fields sent are changed; null is refused for name, code and is_active (omit them instead).
    Archived posts answer 404 until restored (POST /{post_id}/restore); is_deleted is never read from the body.
    """
    current_user = auth.user
    AccessControlService.check_post(auth, Permissions.POST_UPDATE, post_id)
    # A move locks the old and the new group first (groups top down, then the post), like an archive of either
    groups = [auth.tree.post_group.get(post_id), post_data.group_id] if "group_id" in post_data.model_fields_set else []
    started = await lock_groups(session, auth.tree, groups)
    post = await (lock if started else lock_first)(session, select(Post).where(Post.id == post_id))

    if not post or post.is_deleted:
        raise ApiError(ErrorCode.POST_NOT_FOUND)

    update_data = post_data.model_dump(exclude_unset=True)
    if "group_id" in update_data and update_data["group_id"] != post.group_id:
        # A move needs post.update covering the old and the new location (a grant on the post itself covers neither)
        for location in (_location(post.group_id), _location(update_data["group_id"])):
            AccessControlService.check_covers(auth, Permissions.POST_UPDATE, location, "move_post")
        await _check_group(session, update_data["group_id"])
    # Check code uniqueness if being updated
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

    return await _post_response(session, auth, post)


async def _set_post_archived(
        request: Request,
        session: AsyncSession,
        auth: AuthContext,
        post_id: int,
        archived: bool,
) -> PostResponse:
    """Shared body of archive / restore: post.archive at the post, state checks, flag flip, audit row. An archived
    post is restored only through a grant inherited from a group or the root (a grant on the post itself stops
    applying while it is archived)."""
    current_user = auth.user
    AccessControlService.check_post(auth, Permissions.POST_ARCHIVE, post_id)
    # A restore locks the group first (groups before posts): an archive of the group cannot slip in between
    started = await lock_groups(session, auth.tree, [] if archived else [auth.tree.post_group.get(post_id)])
    post = await (lock if started else lock_first)(session, select(Post).where(Post.id == post_id))
    if not post or (archived and post.is_deleted):
        raise ApiError(ErrorCode.POST_NOT_FOUND)
    if not archived and not post.is_deleted:
        raise ApiError(ErrorCode.POST_NOT_ARCHIVED)
    if not archived:
        # Like a group restored under an archived parent: restore the group first
        await _check_group(session, post.group_id)

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
    return await _post_response(session, auth, post)


@router.post(
    PostRoutes.POST_ARCHIVE.value,
    response_model=PostResponse,
    responses=problems(
        AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.POST_WRITE_DENIED, ErrorCode.POST_NOT_FOUND, *GUARDED,
    ),
)
async def archive_post(
        request: Request,
        post_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.POST_VIEW, Permissions.POST_ARCHIVE]))
):
    """Archive a post (is_deleted=true). post.archive must be in effect at the post.

    Nothing is removed and no user row changes. Role assignments made on the post itself stop applying until it is
    restored; assignments inherited from a group or the root keep read access to its data. Its observations and media
    become read-only history: writes answer 409 post_archived, except that callers with post.archive in effect at the
    post (plus observation.view, observation.archive and ownership or observation.manage_any) still archive and
    restore its observations. It cannot become a home post. Hidden from lists unless include_deleted=true.
    Undo with POST /{post_id}/restore. Already archived or unknown: 404.
    """
    return await _set_post_archived(request, session, auth, post_id, archived=True)


@router.post(
    PostRoutes.POST_RESTORE.value,
    response_model=PostResponse,
    responses=problems(
        AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.POST_WRITE_DENIED, ErrorCode.POST_NOT_FOUND,
        ErrorCode.POST_NOT_ARCHIVED, ErrorCode.SCOPE_GROUP_NOT_FOUND, *GUARDED,
    ),
)
async def restore_post(
        request: Request,
        post_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.POST_VIEW, Permissions.POST_ARCHIVE]))
):
    """Restore an archived post (is_deleted=false): role assignments made on it apply again.
    post.archive must be in effect at the post through a group or the root (a grant on the archived post itself
    does not apply). Unknown: 404. Not archived: 409. Its group archived: 404 scope_group_not_found (restore the
    group first, as for a group under an archived parent)."""
    return await _set_post_archived(request, session, auth, post_id, archived=False)
