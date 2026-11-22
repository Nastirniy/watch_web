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
from app.backend.api.shared.helpers.move_helper import admit_move, changed_fields, move_conflict, move_step
from app.backend.api.shared.helpers.scope_group_helper import lock_groups
from app.backend.api.shared.helpers.upload_helper import cancel_pending_uploads, remove_cancelled_parts
from app.backend.api.versioning import PublishedVersion, request_version
from app.backend.services import move_queue, moves
from app.backend.services.media import uploads
from app.backend.models.post import Post
from app.backend.auth.context import AuthContext
from app.backend.api.v1.models.request.posts import PostCreate, PostUpdate
from app.backend.auth.access_control import AccessControlService
from app.backend.auth.move_access import move_requirements
from app.backend.auth.privileges import check_move, check_node_restore
from app.backend.auth.require import require
from app.backend.auth.scope import Node, ROOT
from app.backend.enums.audit import AuditEvent
from app.backend.enums.general import MoveCancelCause, MoveNodeKind, Permissions, StatsGroupBy
from app.backend.config import settings
from app.backend.enums.routes import PostRoutes
from app.backend.api.shared.helpers.audit_helper import client_info
from app.backend.services.audit import create_audit_log
from app.backend.services.events import publish_scope_changed
from app.backend.api.v1.models.query.posts import PostFilterParams
from app.backend.filters.post_filter import PostFilterService
from app.backend.api.v1.models.response.moves import MOVE_ACCEPTED, move_accepted
from app.backend.api.v1.models.response.posts import (
    PostResponse, PostObservationStatsResponse, PostUpdateResponse, posts_to_response,
)
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


async def _store_post(session: AsyncSession, post: Post, audit) -> None:
    """Flush the post, refresh it, write the audit row and the events (`audit()`) and commit ONCE; a code taken by a
    concurrent request after the check hits the unique index"""
    try:
        await session.flush()
        await session.refresh(post)
        await audit()
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
    """Create new post. post.view and post.create must be held at the target group (or above it); a post directly
    under the root needs both at the root (403 permission_required_for_action, action create_post)."""
    current_user = auth.user
    for permission in (Permissions.POST_CREATE, Permissions.POST_VIEW):
        AccessControlService.check_covers(auth, permission, _location(post_data.group_id), "create_post")
    # The group is locked first: an archive of the group waits, so no post appears inside an archived group
    await lock_groups(session, auth.tree, [post_data.group_id])
    await _check_group(session, post_data.group_id)
    # Check if post with code exists
    result = await session.exec(_code_taken(post_data.code))
    if result.first():
        raise ApiError(ErrorCode.POST_CODE_TAKEN)

    post = Post(**post_data.model_dump())
    session.add(post)

    async def audit() -> None:
        await create_audit_log(
            session, AuditEvent.POST_CREATE, scope=Node.post(post.id), resource_id=post.id,
            params={"post_name": post.name, "post_code": post.code},
            user_id=current_user.id,
            new_values=post.model_dump(),
            **client_info(request, auth)
        )
        # At its group: the holders who see the new post are told, and every hub reloads its tree on it, so the
        # post's own events reach its holders at once (a post the tree does not know sits under the root only)
        publish_scope_changed(session, (_location(post.group_id),))

    await _store_post(session, post, audit=audit)

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
        include_archived: bool = Query(False, description="Count archived observations separately; counts only, so observation.archive is not needed"),
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.POST_VIEW, Permissions.OBSERVATION_VIEW]))
):
    """Observation counters of a post by created_at: all time, last 24 hours, and the period
    per hour or per day with every bucket included. not_archived and archived never overlap.

    Note:
        - post.view and observation.view must be in effect at the post (403 post_access_denied), answered before the
          post's existence
    """
    AccessControlService.check_post(auth, Permissions.POST_VIEW, post_id)
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
    response_model=PostUpdateResponse,
    responses={**MOVE_ACCEPTED, **problems(
        AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.POST_ACCESS_DENIED, AccessErrorCode.POST_WRITE_DENIED,
        AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION, AccessErrorCode.GRANT_EXCEEDS_OWN_PERMISSIONS,
        ErrorCode.POST_CODE_TAKEN, ErrorCode.POST_NOT_FOUND, ErrorCode.SCOPE_GROUP_NOT_FOUND,
        ErrorCode.MOVE_PENDING, ErrorCode.MOVE_CONFLICT, ErrorCode.MOVE_NOT_ALONE, *GUARDED,
    )},
)
async def update_post(
        request: Request,
        post_id: int,
        post_data: PostUpdate,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.POST_VIEW, Permissions.POST_UPDATE])),
        version: PublishedVersion = Depends(request_version),
):
    """Update post by ID. post.view (403 post_access_denied) and post.update (403 post_write_denied) must be in effect
    at the post; a move (group_id) also needs post.update and post.view covering both the old and the new location
    (the group or above it; the root for a post directly under it; 403 permission_required_for_action, move_post).
    Every role assignment at the new location and above it that does not already apply at the post comes into
    effect there with the move: the caller must be able to grant each of them at the post, holding every permission
    of its role there before the move, the grants of other users included (403 grant_exceeds_own_permissions with
    the permissions missing; answered after the new group's existence).

    Only the fields sent are changed; null is refused for name, code and is_active (omit them instead).
    Archived posts answer 404 until restored (POST /{post_id}/restore); is_deleted is never read from the body.

    A move states where it thinks the node is: with group_id send expected_group_id, the value the client read (null: directly under the root; presence counts).
    Checked under the move lock, force or not, in this order. The post already at group_id: 200 with already_there true,
    nothing written, the other fields sent included, no audit row. expected_group_id missing or other than where
    the post sits: 409 move_conflict with the current place (current_place root; group with current_group_id; hidden for a
    group outside the caller's scope_group.view) and nothing moves: read the record and decide again. So a client that
    sends the whole record from an old form is refused, not obeyed. group_id sent with the current value is no move: an
    ordinary edit, with no expected_group_id or with the same value there.

    A move during work: with uploads receiving bytes below the node (upload sessions open or completing) the move
    waits. The answer is 202 with the move operation, Location and Retry-After; nothing moves yet, and new uploads
    below the node answer 409 move_pending; chunks, completes and every other write go on. The move runs when nothing
    holds it any more: an upload that ended (finished, aborted, archived, expired) holds nothing, nor does one with no
    bytes for moves.upload_stale_seconds, 300 by default, which is left alone and may go on at the new place after the
    move. The first request that meets the move then runs it, or the worker. The permission checks run again at that
    moment: a move the caller may no longer make ends failed and nothing moves. Follow it at GET /moves/{move_id}; POST
    /moves/{move_id}/cancel cancels it. force true runs it at once and the uploads continue at the new place. The same
    move repeated while it waits answers 202 with that move, from anyone allowed to make it. A node on the way is busy:
    moving a waiting node, anything below it or above it, moving anything into it, or moving its target (or a group
    holding the target) answers 409 move_pending with move_id, force too; cancel that move first. A request that would
    have to wait and changes anything besides the move answers 422 move_not_alone with changed_fields and nothing
    happens: send a move as its own request. Sessions and tokens stay valid; rights at the new place apply from the
    next request (API.md, "A move during work").
    """
    current_user = auth.user
    AccessControlService.check_post(auth, Permissions.POST_VIEW, post_id)
    AccessControlService.check_post(auth, Permissions.POST_UPDATE, post_id)
    # A request that changes the group takes the locks of a move: the moves lock, the old and the new group, the post by
    # id and its pending uploads (services/moves.py), all before the first plain read; the Post entity after them. The
    # current value sent back takes none: the row, under its lock, decides what the request is
    move_locks = None
    if "group_id" in post_data.model_fields_set and moves.takes_move_locks(auth.tree, Node.post(post_id), post_data.group_id):
        move_locks = await moves.lock_for_move(session, auth.tree, Node.post(post_id), post_data.group_id)
    post = await (lock if move_locks else lock_first)(session, select(Post).where(Post.id == post_id))

    if not post or post.is_deleted:
        raise ApiError(ErrorCode.POST_NOT_FOUND)

    step = move_step(post_data, "group_id", "expected_group_id", post.group_id)
    if step is moves.MoveStep.CONFLICT:
        raise move_conflict(auth, post.group_id)
    if step is moves.MoveStep.ALREADY_THERE:
        answer = await _post_response(session, auth, post)
        return PostUpdateResponse(**answer.model_dump(), already_there=True)
    update_data = post_data.model_dump(exclude_unset=True, exclude={"force", "expected_group_id"})
    is_move = step is moves.MoveStep.MOVE
    if is_move:
        # A move needs post.update and post.view covering the old and the new location, as a group or brigade move
        # does (a grant on the post itself covers neither)
        for permission, location in move_requirements(MoveNodeKind.POST, _location(post.group_id),
                                                       _location(update_data["group_id"])):
            AccessControlService.check_covers(auth, permission, location, "move_post")
        await _check_group(session, update_data["group_id"])
        # What the new location puts in effect at the post, read after the locks above (auth/privileges.py)
        await check_move(session, auth, Node.post(post.id), _location(update_data["group_id"]))
    # Check code uniqueness if being updated
    if "code" in update_data:
        existing = await session.exec(_code_taken(update_data["code"], exclude_post_id=post_id))
        if existing.first():
            raise ApiError(ErrorCode.POST_CODE_TAKEN)
    if is_move:
        # Last of the checks: a move that has to wait commits its row here, nothing else changed
        waiting = await admit_move(request, session, auth, Node.post(post.id), post.group_id, update_data["group_id"],
                                   move_locks, post_data.force, changed_fields(post, update_data, "group_id"))
        if waiting is not None:
            return move_accepted(await moves.view_of(session, waiting, auth.tree), version)

    # Store old values for auditing
    old_values = post.model_dump()

    for key, value in update_data.items():
        setattr(post, key, value)
    post.updated_at = datetime.now(timezone.utc)

    session.add(post)

    async def audit() -> None:
        await create_audit_log(
            session, AuditEvent.POST_UPDATE, scope=Node.post(post.id), resource_id=post.id,
            params={"post_name": post.name, "post_code": post.code},
            user_id=current_user.id,
            old_values=old_values,
            new_values=post.model_dump(),
            **client_info(request, auth)
        )
        if is_move:     # an immediate move (nothing held it, or force): to the new place and the old one
            publish_scope_changed(session, (Node.post(post.id), _location(old_values["group_id"])))

    await _store_post(session, post, audit=audit)

    return await _post_response(session, auth, post)


async def _set_post_archived(
        request: Request,
        session: AsyncSession,
        auth: AuthContext,
        post_id: int,
        archived: bool,
) -> PostResponse:
    """Shared body of archive / restore: post.view (403 post_access_denied), then post.archive (403 post_write_denied)
    at the post, state checks, flag flip, audit row. An archived post is restored only through grants inherited from
    a group or the root (a grant on the post itself stops applying while it is archived). An archive cancels the
    pending uploads of the post's observations in its transaction (upload_helper.cancel_pending_uploads)."""
    current_user = auth.user
    AccessControlService.check_post(auth, Permissions.POST_VIEW, post_id)
    AccessControlService.check_post(auth, Permissions.POST_ARCHIVE, post_id)
    # A restore locks the group first (groups before posts): an archive of the group cannot slip in between
    started = await lock_groups(session, auth.tree, [] if archived else [auth.tree.post_group.get(post_id)])
    pending, waiting_moves = [], []
    if archived:
        # The post row by its id (the entity's presence subqueries are a plain read: the snapshot), then its pending
        # upload sessions and its waiting move, before that snapshot: a chunk writer renewing its claim, a cancel or
        # a run of the move wait for the archive
        await (lock if started else lock_first)(session, select(Post.id).where(Post.id == post_id))
        pending = await uploads.lock_pending(session, post_ids=[post_id])
        waiting_moves = await move_queue.lock_waiting(session, [Node.post(post_id)])
        started = True
    post = await (lock if started else lock_first)(session, select(Post).where(Post.id == post_id))
    if not post or (archived and post.is_deleted):
        raise ApiError(ErrorCode.POST_NOT_FOUND)
    if not archived and not post.is_deleted:
        raise ApiError(ErrorCode.POST_NOT_ARCHIVED)
    if not archived:
        # Like a group restored under an archived parent: restore the group first
        await _check_group(session, post.group_id)
        # The assignments made on the post apply again: the caller could grant each of them there
        await check_node_restore(session, auth, Node.post(post.id))

    event = AuditEvent.POST_ARCHIVE if archived else AuditEvent.POST_RESTORE
    old_values = post.model_dump()
    post.is_deleted = archived
    post.updated_at = datetime.now(timezone.utc)
    session.add(post)
    await create_audit_log(
        session, event, scope=Node.post(post.id), resource_id=post.id,
        params={"post_name": post.name, "post_code": post.code},
        user_id=current_user.id,
        old_values=old_values,
        new_values=post.model_dump(),
        **client_info(request, auth)
    )
    cancelled = await cancel_pending_uploads(request, session, auth, pending, ErrorCode.POST_ARCHIVED)
    await moves.cancel_locked(session, waiting_moves, MoveCancelCause.NODE_ARCHIVED, current_user.id,
                              client_info(request, auth))
    publish_scope_changed(session, (Node.post(post.id),))
    await session.commit()
    await remove_cancelled_parts(session, cancelled)
    await session.refresh(post)
    return await _post_response(session, auth, post)


@router.post(
    PostRoutes.POST_ARCHIVE.value,
    response_model=PostResponse,
    responses=problems(
        AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.POST_ACCESS_DENIED, AccessErrorCode.POST_WRITE_DENIED,
        ErrorCode.POST_NOT_FOUND, *GUARDED,
    ),
)
async def archive_post(
        request: Request,
        post_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.POST_VIEW, Permissions.POST_ARCHIVE]))
):
    """Archive a post (is_deleted=true). post.view and post.archive must be in effect at the post.

    Nothing is removed and no user row changes. Role assignments made on the post itself stop applying until it is
    restored; assignments inherited from a group or the root keep read access to its data. Its observations and media
    become read-only history: writes answer 409 post_archived, except that callers with post.archive in effect at the
    post (plus observation.view, observation.archive and ownership or observation.manage_any) still archive and
    restore its observations. It cannot become a home post. Hidden from lists unless include_deleted=true. Every
    pending resumable upload of its observations is cancelled at once (status aborted, error post_archived; the
    received bytes are deleted); a restore does not reopen it. A waiting move of the post is cancelled too. Undo with
    POST /{post_id}/restore. Already archived or unknown: 404.
    """
    return await _set_post_archived(request, session, auth, post_id, archived=True)


@router.post(
    PostRoutes.POST_RESTORE.value,
    response_model=PostResponse,
    responses=problems(
        AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.POST_ACCESS_DENIED, AccessErrorCode.POST_WRITE_DENIED,
        AccessErrorCode.GRANT_EXCEEDS_OWN_PERMISSIONS, ErrorCode.POST_NOT_FOUND, ErrorCode.POST_NOT_ARCHIVED,
        ErrorCode.SCOPE_GROUP_NOT_FOUND, *GUARDED,
    ),
)
async def restore_post(
        request: Request,
        post_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.POST_VIEW, Permissions.POST_ARCHIVE]))
):
    """Restore an archived post (is_deleted=false): role assignments made on it apply again.
    post.view and post.archive must be in effect at the post through a group or the root (a grant on the archived
    post itself does not apply). Unknown: 404. Not archived: 409. Its group archived: 404 scope_group_not_found (restore the
    group first, as for a group under an archived parent). The role assignments made on the post apply again: the
    caller must hold every permission of each of their roles at the post (403 grant_exceeds_own_permissions), as for
    a role's restore."""
    return await _set_post_archived(request, session, auth, post_id, archived=False)
