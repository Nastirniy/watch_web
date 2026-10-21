"""Corps: the top units of the organisation, scope groups of kind corps directly under the root.

A thin facade over the scope group rules (services/scope_groups.py) and the unit helpers (services/units.py): a corps
is a scope group, so roles given at it cover its brigades and posts, and /scope-groups shows it too (kind corps).
"""
from dataclasses import asdict, replace
from datetime import datetime, timezone
from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, Path, Query, Request, status
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.api.shared.helpers.db_errors import unique_violation_as
from app.backend.api.shared.helpers.scope_group_helper import (
    unit_observation_stats, archive_unit, audit_group, get_unit, name_taken, parent_node, restore_unit,
)
from app.backend.api.v1.models.query.corps import UnitFilterParams
from app.backend.api.v1.models.query.posts import PostFilterParams
from app.backend.api.v1.models.request.corps import CorpsCreate, CorpsUpdate
from app.backend.api.v1.models.response.brigades import BrigadeResponse, brigade_list_to_response
from app.backend.api.v1.models.response.corps import (
    CorpsResponse, UnitObservationStatsResponse, corps_list_to_response, corps_to_response,
)
from app.backend.api.v1.models.response.posts import PostResponse, posts_to_response
from app.backend.auth.access_control import AccessControlService
from app.backend.auth.context import AuthContext, load_scope_tree
from app.backend.auth.require import require
from app.backend.auth.scope import ROOT
from app.backend.config import settings
from app.backend.database import get_session
from app.backend.enums.errors import AccessErrorCode, ErrorCode
from app.backend.enums.general import Permissions, ScopeGroupKind, StatsGroupBy, UnitPlacement
from app.backend.enums.routes import CorpsRoutes
from app.backend.errors.openapi import GUARDED, problems
from app.backend.errors.problems import ApiError
from app.backend.filters.post_filter import PostFilterService
from app.backend.filters.unit_filter import UnitFilterService
from app.backend.models.post import Post
from app.backend.models.scope_group import ScopeGroup
from app.backend.services.units import posts_within
from app.backend.utils.validators import UtcDatetime

router = APIRouter(prefix=CorpsRoutes.BASE.value, tags=[CorpsRoutes.tag()])

CORPS = ScopeGroupKind.CORPS


async def _fresh(session: AsyncSession, auth: AuthContext) -> AuthContext:
    """The caller with the tree as committed (the counts of an answer after an archive or restore)"""
    return replace(auth, tree=await load_scope_tree(session))


@router.get(
    CorpsRoutes.ROOT.value,
    response_model=List[CorpsResponse],
    responses=problems(AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION, *GUARDED),
)
async def list_corps(
        filters: Annotated[UnitFilterParams, Query()],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.SCOPE_GROUP_VIEW]))
):
    """List corps with filters: the corps inside the subtrees where the caller holds scope_group.view (every corps
    with it at the root). Archived ones only with include_deleted (requires scope_group.archive), and only where
    scope_group.archive is in effect."""
    archived = AccessControlService.archived_group_scope(auth, filters, Permissions.SCOPE_GROUP_ARCHIVE)
    query = UnitFilterService.apply_filters(
        select(ScopeGroup), filters, CORPS, auth.groups_with(Permissions.SCOPE_GROUP_VIEW), archived_ids=archived,
    )
    return corps_list_to_response((await session.exec(query)).all(), auth)


@router.post(
    CorpsRoutes.ROOT.value,
    response_model=CorpsResponse,
    status_code=status.HTTP_201_CREATED,
    responses=problems(ErrorCode.SCOPE_GROUP_NAME_TAKEN, AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION, *GUARDED),
)
async def create_corps(
        request: Request,
        data: CorpsCreate,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.SCOPE_GROUP_VIEW, Permissions.SCOPE_GROUP_CREATE]))
):
    """Create a corps directly under the root: scope_group.view and scope_group.create at the root (403
    permission_required_for_action otherwise, before the name check). Names are unique among the corps and the plain
    groups of the root, case-insensitive."""
    for permission in (Permissions.SCOPE_GROUP_VIEW, Permissions.SCOPE_GROUP_CREATE):
        AccessControlService.check_covers(auth, permission, ROOT, "create_corps")
    if await name_taken(session, data.name, None):
        raise ApiError(ErrorCode.SCOPE_GROUP_NAME_TAKEN)

    corps = ScopeGroup(name=data.name, description=data.description, parent_id=None, parent_key=0, kind=CORPS.value)
    session.add(corps)
    # The audit row joins the change: one commit (the name index is hit at the flush)
    async with unique_violation_as(session, ErrorCode.SCOPE_GROUP_NAME_TAKEN):
        await session.flush()
        await session.refresh(corps)
        await audit_group(request, session, auth, "create", corps, None)
        await session.commit()
    return corps_to_response(corps, auth)


@router.get(
    CorpsRoutes.CORPS_BY_ID.value,
    response_model=CorpsResponse,
    responses=problems(ErrorCode.SCOPE_GROUP_NOT_FOUND, *GUARDED),
)
async def get_corps(
        corps_id: Annotated[int, Path(ge=1)],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.SCOPE_GROUP_VIEW]))
):
    """Get a corps by ID. Archived, another kind of group, or outside the caller's scope_group.view: 404 like an
    unknown one."""
    return corps_to_response(await get_unit(session, auth, CORPS, corps_id), auth)


@router.patch(
    CorpsRoutes.CORPS_BY_ID.value,
    response_model=CorpsResponse,
    responses=problems(
        ErrorCode.SCOPE_GROUP_NOT_FOUND, ErrorCode.SCOPE_GROUP_NAME_TAKEN, AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION,
        *GUARDED,
    ),
)
async def update_corps(
        request: Request,
        corps_id: Annotated[int, Path(ge=1)],
        data: CorpsUpdate,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.SCOPE_GROUP_VIEW, Permissions.SCOPE_GROUP_UPDATE]))
):
    """Rename or describe a corps; only the fields sent change. Archived, another kind of group, or outside the
    caller's scope_group.view: 404. scope_group.update must cover the parent of the corps, the root (403
    permission_required_for_action, action update_corps): a holder at the corps manages its brigades, not the corps.
    A new name also needs scope_group.view covering the root (same answer): the name is checked against the root."""
    corps = await get_unit(session, auth, CORPS, corps_id, lock=True)
    AccessControlService.check_covers(auth, Permissions.SCOPE_GROUP_UPDATE, parent_node(corps), "update_corps")
    update_data = data.model_dump(exclude_unset=True)
    if "name" in update_data:
        # The name is checked against the names beside the corps (409): scope_group.view at the root as well, or the
        # answer tells which root names exist
        AccessControlService.check_covers(auth, Permissions.SCOPE_GROUP_VIEW, parent_node(corps), "update_corps")
    old_values = corps.model_dump()
    if "name" in update_data and await name_taken(session, update_data["name"], None, exclude_id=corps.id):
        raise ApiError(ErrorCode.SCOPE_GROUP_NAME_TAKEN)

    for key, value in update_data.items():
        setattr(corps, key, value)
    corps.updated_at = datetime.now(timezone.utc)
    session.add(corps)
    # The audit row joins the change: one commit (the name index is hit at the flush)
    async with unique_violation_as(session, ErrorCode.SCOPE_GROUP_NAME_TAKEN):
        await session.flush()
        await session.refresh(corps)
        await audit_group(request, session, auth, "update", corps, old_values)
        await session.commit()
    return corps_to_response(corps, auth)


@router.post(
    CorpsRoutes.CORPS_ARCHIVE.value,
    response_model=CorpsResponse,
    responses=problems(
        ErrorCode.SCOPE_GROUP_NOT_FOUND, ErrorCode.CORPS_HAS_BRIGADES, AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION, *GUARDED,
    ),
)
async def archive_corps(
        request: Request,
        corps_id: Annotated[int, Path(ge=1)],
        cascade: bool = Query(False, description="Archive the brigades and posts of the corps too; without it the corps must have no active brigade and its posts move under the root"),
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.SCOPE_GROUP_VIEW, Permissions.SCOPE_GROUP_ARCHIVE]))
):
    """Archive a corps (is_deleted=true); the answer is the corps. scope_group.archive must cover its parent, the
    root (a holder at the corps manages its brigades, not the corps). Without
    cascade: a corps with an active brigade answers 409 corps_has_brigades (move or archive its brigades first), the
    active posts directly in it leave it (directly under the root), which needs post.update and post.view covering the
    corps and the root. With cascade=true: its active brigades and every active post below are archived too, which
    needs post.archive and post.view covering the corps. No post permission is asked when no active post would change.
    Missing permissions: 403 permission_required_for_action, nothing changes. Holders of a role at the
    corps lose the posts that left it. Archived, unknown, another kind or outside scope_group.view: 404. Undo with
    POST /{corps_id}/restore (the corps only)"""
    corps = await archive_unit(request, session, auth, CORPS, corps_id, cascade)
    return corps_to_response(corps, await _fresh(session, auth))


@router.post(
    CorpsRoutes.CORPS_RESTORE.value,
    response_model=CorpsResponse,
    responses=problems(
        ErrorCode.SCOPE_GROUP_NOT_FOUND, ErrorCode.SCOPE_GROUP_NOT_ARCHIVED,
        AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION, *GUARDED,
    ),
)
async def restore_corps(
        request: Request,
        corps_id: Annotated[int, Path(ge=1)],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.SCOPE_GROUP_VIEW, Permissions.SCOPE_GROUP_ARCHIVE]))
):
    """Restore an archived corps (is_deleted=false): the corps only; its archived brigades and posts are
    listed with include_deleted (GET /{corps_id}/brigades, /{corps_id}/posts) and restored one by one.
    scope_group.archive must cover its parent, the root (403). Not archived: 409; unknown, another kind or outside
    scope_group.view: 404."""
    corps = await restore_unit(request, session, auth, CORPS, corps_id)
    return corps_to_response(corps, await _fresh(session, auth))


@router.get(
    CorpsRoutes.CORPS_BRIGADES.value,
    response_model=List[BrigadeResponse],
    responses=problems(ErrorCode.SCOPE_GROUP_NOT_FOUND, AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION, *GUARDED),
)
async def list_corps_brigades(
        corps_id: Annotated[int, Path(ge=1)],
        filters: Annotated[UnitFilterParams, Query()],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.SCOPE_GROUP_VIEW]))
):
    """The brigades of a corps the caller sees (scope_group.view), with the brigade filters. Archived ones only with
    include_deleted (requires scope_group.archive), and only where scope_group.archive is in effect. The corps: 404
    as on GET /corps/{corps_id}."""
    archived = AccessControlService.archived_group_scope(auth, filters, Permissions.SCOPE_GROUP_ARCHIVE)
    corps = await get_unit(session, auth, CORPS, corps_id)
    criteria = replace(UnitFilterService.criteria(filters), corps_id=corps.id)
    query = UnitFilterService.apply_filters(
        select(ScopeGroup), criteria, ScopeGroupKind.BRIGADE, auth.groups_with(Permissions.SCOPE_GROUP_VIEW), archived_ids=archived,
    )
    return brigade_list_to_response((await session.exec(query)).all(), auth)


@router.get(
    CorpsRoutes.CORPS_POSTS.value,
    response_model=List[PostResponse],
    responses=problems(ErrorCode.SCOPE_GROUP_NOT_FOUND, AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION, *GUARDED),
)
async def list_corps_posts(
        corps_id: Annotated[int, Path(ge=1)],
        filters: Annotated[PostFilterParams, Query()],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.SCOPE_GROUP_VIEW, Permissions.POST_VIEW]))
):
    """The posts of a corps at any depth (its brigades included) where post.view is in effect, with the post
    filters. Archived posts only with include_deleted (requires post.archive), and only where post.archive is in
    effect. The corps: 404 as on GET /corps/{corps_id}."""
    archived = AccessControlService.archived_scope(auth, filters, Permissions.POST_ARCHIVE)
    corps = await get_unit(session, auth, CORPS, corps_id)
    query = PostFilterService.apply_filters(
        select(Post), filters, AccessControlService.scope_for(auth, Permissions.POST_VIEW), archived_post_ids=archived,
        unit_post_ids=posts_within(auth.tree, CORPS, corps.id, filters),
    )
    return await posts_to_response(session, auth.tree, list((await session.exec(query)).all()))


@router.get(
    CorpsRoutes.CORPS_OBSERVATION_STATS.value,
    response_model=UnitObservationStatsResponse,
    responses=problems(
        ErrorCode.PERIOD_REVERSED, ErrorCode.PERIOD_TOO_LONG, ErrorCode.STATS_PERIOD_OUT_OF_RANGE,
        ErrorCode.UNKNOWN_TIME_ZONE, ErrorCode.SCOPE_GROUP_NOT_FOUND,
        *GUARDED,
    ),
)
async def get_corps_observation_stats(
        corps_id: Annotated[int, Path(ge=1)],
        placement: UnitPlacement = Query(UnitPlacement.RECORDED, description="recorded: the observations created while their post was in the corps; current: the observations of the posts in it now (archived posts left out)"),
        date_from: Optional[UtcDatetime] = Query(None, description="Period start, created_at (default: date_to minus 30 days; naive values are UTC)"),
        date_to: Optional[UtcDatetime] = Query(None, description="Period end, created_at (default: now)"),
        group_by: StatsGroupBy = Query(StatsGroupBy.DAY, description="Bucket size: hour (period up to 31 days) or day (up to 366 days)"),
        tz: Optional[str] = Query(None, max_length=64, description="IANA time zone of the buckets (default: server time zone setting)"),
        include_archived: bool = Query(False, description="Count archived observations separately; counts only, so observation.view is enough"),
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.OBSERVATION_VIEW]))
):
    """Observation counters of a corps by created_at, as for a post: all time, last 24 hours, and the period per
    hour or per day with every bucket included; not_archived and archived never overlap. Only the posts where
    observation.view is in effect are counted. The corps must lie where the caller holds scope_group.view or
    observation.view, or have a post under it now where observation.view is in effect (a brigade's commander counts
    its corps), otherwise 404 like an unknown id or a group of another kind. An archived corps keeps answering its
    history."""
    stats = await unit_observation_stats(
        session, auth, CORPS, corps_id, placement, date_from, date_to, group_by, tz or settings.timezone.timezone,
        include_archived,
    )
    return UnitObservationStatsResponse(
        unit_id=corps_id, placement=placement, **asdict(stats),
    )
