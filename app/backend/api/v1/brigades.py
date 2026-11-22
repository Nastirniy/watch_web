"""Brigades: units inside a corps, scope groups of kind brigade.

A thin facade over the scope group rules (services/scope_groups.py) and the unit helpers (services/units.py). A
brigade sits directly in a corps (ALLOWED_PARENT); moving it to another corps takes its posts along, like a group
move.
"""
from dataclasses import asdict, replace
from datetime import datetime, timezone
from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, Path, Query, Request, status
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.api.shared.helpers.db_errors import unique_violation_as
from app.backend.api.shared.helpers.move_helper import admit_move, changed_fields, move_conflict, move_step
from app.backend.api.shared.helpers.scope_group_helper import (
    unit_observation_stats, archive_unit, audit_group, check_visible, get_unit, lock_groups, lock_unit, name_taken,
    parent_location, restore_unit,
)
from app.backend.api.v1.models.query.brigades import BrigadeFilterParams
from app.backend.api.v1.models.query.posts import PostFilterParams
from app.backend.api.v1.models.request.brigades import BrigadeCreate, BrigadeUpdate
from app.backend.api.v1.models.response.brigades import (
    BrigadeResponse, BrigadeUpdateResponse, brigade_list_to_response, brigade_to_response,
)
from app.backend.api.v1.models.response.corps import UnitObservationStatsResponse
from app.backend.api.v1.models.response.moves import MOVE_ACCEPTED, move_accepted
from app.backend.api.v1.models.response.posts import PostResponse, posts_to_response
from app.backend.api.versioning import PublishedVersion, request_version
from app.backend.auth.access_control import AccessControlService
from app.backend.auth.context import AuthContext, load_scope_tree
from app.backend.auth.move_access import move_requirements
from app.backend.auth.privileges import check_move
from app.backend.auth.require import require
from app.backend.auth.scope import Node
from app.backend.config import settings
from app.backend.database import get_session
from app.backend.enums.errors import AccessErrorCode, ErrorCode
from app.backend.enums.general import MoveNodeKind, Permissions, ScopeGroupKind, StatsGroupBy, UnitPlacement
from app.backend.enums.routes import BrigadeRoutes
from app.backend.errors.coded import CodedError
from app.backend.errors.openapi import GUARDED, problems
from app.backend.errors.problems import ApiError
from app.backend.filters.post_filter import PostFilterService
from app.backend.filters.unit_filter import UnitFilterService
from app.backend.models.post import Post
from app.backend.models.scope_group import ScopeGroup
from app.backend.services import moves
from app.backend.services import scope_groups as groups_service
from app.backend.services.events import publish_scope_changed
from app.backend.services.units import posts_within
from app.backend.utils.validators import UtcDatetime

router = APIRouter(prefix=BrigadeRoutes.BASE.value, tags=[BrigadeRoutes.tag()])

BRIGADE = ScopeGroupKind.BRIGADE


async def _fresh(session: AsyncSession, auth: AuthContext) -> AuthContext:
    """The caller with the tree as committed (the counts of an answer after an archive or restore)"""
    return replace(auth, tree=await load_scope_tree(session))

# Where a brigade may go: the corps is unknown, archived or outside scope_group.view, or not a corps
PLACEMENT_PROBLEMS = (ErrorCode.SCOPE_GROUP_NOT_FOUND, ErrorCode.SCOPE_GROUP_PARENT_NOT_ALLOWED)


async def _check_corps(session: AsyncSession, brigade_id: Optional[int], corps_id: int) -> None:
    """The destination is a corps (404 unknown, 409 another kind: checked first, so a brigade or a deep plain group
    never reaches the cycle and depth rules) and live (404 archived)"""
    try:
        await groups_service.check_placement(session, BRIGADE, corps_id)
        await groups_service.check_new_parent(session, brigade_id, corps_id)
    except CodedError as exc:
        raise ApiError.from_coded(exc)


@router.get(
    BrigadeRoutes.ROOT.value,
    response_model=List[BrigadeResponse],
    responses=problems(AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION, *GUARDED),
)
async def list_brigades(
        filters: Annotated[BrigadeFilterParams, Query()],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.SCOPE_GROUP_VIEW]))
):
    """List brigades with filters (corps_id: the brigades of one corps): the brigades inside the subtrees where the
    caller holds scope_group.view. Archived ones only with include_deleted (requires scope_group.archive), and only
    where scope_group.archive is in effect."""
    archived = AccessControlService.archived_group_scope(auth, filters, Permissions.SCOPE_GROUP_ARCHIVE)
    query = UnitFilterService.apply_filters(
        select(ScopeGroup), filters, BRIGADE, auth.groups_with(Permissions.SCOPE_GROUP_VIEW), archived_ids=archived,
    )
    return brigade_list_to_response((await session.exec(query)).all(), auth)


@router.post(
    BrigadeRoutes.ROOT.value,
    response_model=BrigadeResponse,
    status_code=status.HTTP_201_CREATED,
    responses=problems(
        *PLACEMENT_PROBLEMS, ErrorCode.SCOPE_GROUP_NAME_TAKEN, AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION, *GUARDED,
    ),
)
async def create_brigade(
        request: Request,
        data: BrigadeCreate,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.SCOPE_GROUP_VIEW, Permissions.SCOPE_GROUP_CREATE]))
):
    """Create a brigade in a corps: scope_group.create covering the corps (403 permission_required_for_action).
    The corps unknown, archived or outside the caller's scope_group.view: 404; corps_id of a brigade or a plain
    group: 409 scope_group_parent_not_allowed. Names are unique within one corps, case-insensitive."""
    check_visible(auth, data.corps_id)
    AccessControlService.check_covers(auth, Permissions.SCOPE_GROUP_CREATE, Node.group(data.corps_id), "create_brigade")
    # The corps is locked first: an archive of the corps waits, so no brigade appears inside an archived corps (a
    # corps the request's tree does not know is not locked: the checks below answer 404)
    await lock_groups(session, auth.tree, [data.corps_id])
    await _check_corps(session, None, data.corps_id)
    if await name_taken(session, data.name, data.corps_id):
        raise ApiError(ErrorCode.SCOPE_GROUP_NAME_TAKEN)

    brigade = ScopeGroup(name=data.name, description=data.description, parent_id=data.corps_id,
                         parent_key=data.corps_id, kind=BRIGADE.value)
    session.add(brigade)
    # The audit row joins the change: one commit (the name index is hit at the flush)
    async with unique_violation_as(session, ErrorCode.SCOPE_GROUP_NAME_TAKEN):
        await session.flush()
        await session.refresh(brigade)
        await audit_group(request, session, auth, "create", brigade, None)
        await session.commit()
    return brigade_to_response(brigade, auth)


@router.get(
    BrigadeRoutes.BRIGADE_BY_ID.value,
    response_model=BrigadeResponse,
    responses=problems(ErrorCode.SCOPE_GROUP_NOT_FOUND, *GUARDED),
)
async def get_brigade(
        brigade_id: Annotated[int, Path(ge=1)],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.SCOPE_GROUP_VIEW]))
):
    """Get a brigade by ID. Archived, another kind of group, or outside the caller's scope_group.view: 404 like an
    unknown one."""
    return brigade_to_response(await get_unit(session, auth, BRIGADE, brigade_id), auth)


@router.patch(
    BrigadeRoutes.BRIGADE_BY_ID.value,
    response_model=BrigadeUpdateResponse,
    responses={**MOVE_ACCEPTED, **problems(
        *PLACEMENT_PROBLEMS, ErrorCode.SCOPE_GROUP_NAME_TAKEN, AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION,
        AccessErrorCode.GRANT_EXCEEDS_OWN_PERMISSIONS, ErrorCode.MOVE_PENDING, ErrorCode.MOVE_CONFLICT,
        ErrorCode.MOVE_NOT_ALONE, *GUARDED,
    )},
)
async def update_brigade(
        request: Request,
        brigade_id: Annotated[int, Path(ge=1)],
        data: BrigadeUpdate,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.SCOPE_GROUP_VIEW, Permissions.SCOPE_GROUP_UPDATE])),
        version: PublishedVersion = Depends(request_version),
):
    """Rename, describe or move a brigade; only the fields sent change. Archived, another kind of group, or outside
    the caller's scope_group.view: 404. scope_group.update must cover its corps (403 permission_required_for_action,
    action update_brigade): a holder at the brigade does not manage the brigade itself. A new name without a move also
    needs scope_group.view covering the corps (same answer): the name is checked against its brigades. A move (corps_id other than
    its corps; its own corps sent back is no move and answers as without it) needs
    scope_group.view and scope_group.update covering the old and the new corps and, as it takes every post of the brigade along, post.view
    and post.update covering both (403 permission_required_for_action, action move_brigade), answered before the
    existence of the new corps; the new corps unknown, archived or outside scope_group.view: 404, not a corps: 409
    scope_group_parent_not_allowed. Every role assignment at the new corps that does not already apply at the brigade
    comes into effect for it and its posts: the caller must be able to grant each of them at the brigade, holding
    every permission of its role there before the move, the grants of other users included (403
    grant_exceeds_own_permissions, after the corps checks). Nothing changes on a refusal.

    A move states where it thinks the node is: with corps_id send expected_corps_id, the value the client read.
    Checked under the move lock, force or not, in this order. The brigade already at corps_id: 200 with already_there true,
    nothing written, the other fields sent included, no audit row. expected_corps_id missing or other than where
    the brigade sits: 409 move_conflict with the current place (current_place root; group with current_group_id; hidden for a
    group outside the caller's scope_group.view) and nothing moves: read the record and decide again. So a client that
    sends the whole record from an old form is refused, not obeyed. corps_id sent with the current value is no move: an
    ordinary edit, with no expected_corps_id or with the same value there.

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
    update_data = data.model_dump(exclude_unset=True, exclude={"force", "expected_corps_id"})
    # The current corps sent back is no move: only another corps must be visible (404 before any lock)
    if "corps_id" in update_data and update_data["corps_id"] != auth.tree.group_parent.get(brigade_id):
        check_visible(auth, update_data["corps_id"])
    # A request that changes the corps takes the locks of a move, everything below the brigade included
    # (services/moves.py). The current corps sent back takes none: the row decides
    move_locks = None
    if "corps_id" in update_data and moves.takes_move_locks(auth.tree, Node.group(brigade_id), update_data["corps_id"]):
        check_visible(auth, brigade_id)
        move_locks = await moves.lock_for_move(session, auth.tree, Node.group(brigade_id), update_data["corps_id"])
    # Groups top down: the old and the new corps, then the brigade
    brigade = await lock_unit(session, auth, BRIGADE, brigade_id, also=[update_data.get("corps_id") if move_locks else None],
                              started=move_locks is not None)
    old_values = brigade.model_dump()

    new_corps = update_data.get("corps_id", brigade.parent_id)
    step = move_step(data, "corps_id", "expected_corps_id", brigade.parent_id)
    moving = step is moves.MoveStep.MOVE
    if moving:
        check_visible(auth, new_corps)     # the brigade moved since the tree was read: decided by the locked row
        # X.view AND X.update at both ends: the new corps is seen by check_visible, the old one here
        for permission, location in move_requirements(MoveNodeKind.GROUP, parent_location(brigade.parent_id),
                                                      parent_location(new_corps)):
            AccessControlService.check_covers(auth, permission, location, "move_brigade")
    else:
        AccessControlService.check_covers(auth, Permissions.SCOPE_GROUP_UPDATE, parent_location(brigade.parent_id),
                                          "update_brigade")
        if step is moves.MoveStep.CONFLICT:
            raise move_conflict(auth, brigade.parent_id)
        if step is moves.MoveStep.ALREADY_THERE:
            return BrigadeUpdateResponse(**brigade_to_response(brigade, auth).model_dump(), already_there=True)
    if "name" in update_data and not moving:
        # The name is checked against the brigades of the corps (409): scope_group.view at the corps as well
        AccessControlService.check_covers(auth, Permissions.SCOPE_GROUP_VIEW, parent_location(brigade.parent_id),
                                          "update_brigade")
    if moving:
        await _check_corps(session, brigade.id, new_corps)
        # What the new corps puts in effect at the brigade, read after lock_unit's locks (auth/privileges.py)
        await check_move(session, auth, Node.group(brigade.id), Node.group(new_corps))
    if ("corps_id" in update_data or "name" in update_data) and await name_taken(
            session, update_data.get("name", brigade.name), new_corps, exclude_id=brigade.id):
        raise ApiError(ErrorCode.SCOPE_GROUP_NAME_TAKEN)
    if moving:
        # Last of the checks: a move that has to wait commits its row here, nothing else changed
        waiting = await admit_move(request, session, auth, Node.group(brigade.id), brigade.parent_id, new_corps, move_locks,
                                   data.force, changed_fields(brigade, update_data, "corps_id"))
        if waiting is not None:
            return move_accepted(await moves.view_of(session, waiting, auth.tree), version)

    for key, value in update_data.items():
        if key != "corps_id":
            setattr(brigade, key, value)
    brigade.parent_id = new_corps
    brigade.parent_key = new_corps
    brigade.updated_at = datetime.now(timezone.utc)
    session.add(brigade)
    # The audit row joins the change: one commit (the name index is hit at the flush)
    async with unique_violation_as(session, ErrorCode.SCOPE_GROUP_NAME_TAKEN):
        await session.flush()
        await session.refresh(brigade)
        await audit_group(request, session, auth, "update", brigade, old_values)
        if moving:      # an immediate move: to the new corps and the old one
            publish_scope_changed(session, (Node.group(brigade.id), parent_location(old_values["parent_id"])))
        await session.commit()
    return brigade_to_response(brigade, await _fresh(session, auth))   # a move changed what the brigade holds


@router.post(
    BrigadeRoutes.BRIGADE_ARCHIVE.value,
    response_model=BrigadeResponse,
    responses=problems(
        ErrorCode.SCOPE_GROUP_NOT_FOUND, AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION, *GUARDED,
    ),
)
async def archive_brigade(
        request: Request,
        brigade_id: Annotated[int, Path(ge=1)],
        cascade: bool = Query(False, description="Archive the posts of the brigade too; without it they move into its corps"),
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.SCOPE_GROUP_VIEW, Permissions.SCOPE_GROUP_ARCHIVE]))
):
    """Archive a brigade (is_deleted=true); the answer is the brigade. scope_group.archive must cover its corps.
    Without cascade its active posts move into its corps, which needs post.update and post.view covering the brigade
    and the corps; with cascade=true they are archived, which needs post.archive and post.view covering the brigade,
    and every pending resumable upload of their observations is cancelled (status aborted, error post_archived).
    A waiting move of the brigade, and with cascade of its posts, is cancelled. No post permission is asked when the
    brigade has no active post.
    Missing permissions: 403 permission_required_for_action, nothing changes. Holders of a role at the brigade lose its posts. Archived,
    unknown, another kind or outside scope_group.view: 404. Undo with POST /{brigade_id}/restore (the brigade only)"""
    brigade = await archive_unit(request, session, auth, BRIGADE, brigade_id, cascade)
    return brigade_to_response(brigade, await _fresh(session, auth))


@router.post(
    BrigadeRoutes.BRIGADE_RESTORE.value,
    response_model=BrigadeResponse,
    responses=problems(
        ErrorCode.SCOPE_GROUP_NOT_FOUND, ErrorCode.SCOPE_GROUP_NOT_ARCHIVED,
        AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION, AccessErrorCode.GRANT_EXCEEDS_OWN_PERMISSIONS, *GUARDED,
    ),
)
async def restore_brigade(
        request: Request,
        brigade_id: Annotated[int, Path(ge=1)],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.SCOPE_GROUP_VIEW, Permissions.SCOPE_GROUP_ARCHIVE]))
):
    """Restore an archived brigade (is_deleted=false) in its corps: the brigade only; its archived posts are
    listed with include_deleted (GET /{brigade_id}/posts) and restored one by one. scope_group.archive must cover its
    corps (403). Not archived: 409; its corps archived (restore the corps first), unknown, another kind or outside
    scope_group.view: 404. The role assignments made at the brigade apply again: the caller must hold every
    permission of each of their roles at the brigade (403 grant_exceeds_own_permissions)."""
    brigade = await restore_unit(request, session, auth, BRIGADE, brigade_id)
    return brigade_to_response(brigade, await _fresh(session, auth))


@router.get(
    BrigadeRoutes.BRIGADE_POSTS.value,
    response_model=List[PostResponse],
    responses=problems(ErrorCode.SCOPE_GROUP_NOT_FOUND, AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION, *GUARDED),
)
async def list_brigade_posts(
        brigade_id: Annotated[int, Path(ge=1)],
        filters: Annotated[PostFilterParams, Query()],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.SCOPE_GROUP_VIEW, Permissions.POST_VIEW]))
):
    """The posts of a brigade where post.view is in effect, with the post filters. Archived posts only with
    include_deleted (requires post.archive), and only where post.archive is in effect. The brigade: 404 as on
    GET /brigades/{brigade_id}."""
    archived = AccessControlService.archived_scope(auth, filters, Permissions.POST_ARCHIVE)
    brigade = await get_unit(session, auth, BRIGADE, brigade_id)
    query = PostFilterService.apply_filters(
        select(Post), filters, AccessControlService.scope_for(auth, Permissions.POST_VIEW), archived_post_ids=archived,
        unit_post_ids=posts_within(auth.tree, BRIGADE, brigade.id, filters),
    )
    return await posts_to_response(session, auth.tree, list((await session.exec(query)).all()))


@router.get(
    BrigadeRoutes.BRIGADE_OBSERVATION_STATS.value,
    response_model=UnitObservationStatsResponse,
    responses=problems(
        ErrorCode.PERIOD_REVERSED, ErrorCode.PERIOD_TOO_LONG, ErrorCode.STATS_PERIOD_OUT_OF_RANGE,
        ErrorCode.UNKNOWN_TIME_ZONE, ErrorCode.SCOPE_GROUP_NOT_FOUND,
        *GUARDED,
    ),
)
async def get_brigade_observation_stats(
        brigade_id: Annotated[int, Path(ge=1)],
        placement: UnitPlacement = Query(UnitPlacement.RECORDED, description="recorded: the observations created while their post was in the brigade; current: the observations of the posts in it now (archived posts left out)"),
        date_from: Optional[UtcDatetime] = Query(None, description="Period start, created_at (default: date_to minus 30 days; naive values are UTC)"),
        date_to: Optional[UtcDatetime] = Query(None, description="Period end, created_at (default: now)"),
        group_by: StatsGroupBy = Query(StatsGroupBy.DAY, description="Bucket size: hour (period up to 31 days) or day (up to 366 days)"),
        tz: Optional[str] = Query(None, max_length=64, description="IANA time zone of the buckets (default: server time zone setting)"),
        include_archived: bool = Query(False, description="Count archived observations separately; counts only, so observation.view is enough"),
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.OBSERVATION_VIEW]))
):
    """Observation counters of a brigade by created_at, as for a post: all time, last 24 hours, and the period per
    hour or per day with every bucket included; not_archived and archived never overlap. Only the posts where
    observation.view is in effect are counted. The brigade must lie where the caller holds scope_group.view or
    observation.view, or have a post under it now where observation.view is in effect (the viewer of one post
    counts its brigade), otherwise 404 like an unknown id or a group of another kind. An archived brigade keeps answering its
    history."""
    stats = await unit_observation_stats(
        session, auth, BRIGADE, brigade_id, placement, date_from, date_to, group_by, tz or settings.timezone.timezone,
        include_archived,
    )
    return UnitObservationStatsResponse(
        unit_id=brigade_id, placement=placement, **asdict(stats),
    )
