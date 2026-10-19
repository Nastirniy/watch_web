"""Brigades: units inside a corps, scope groups of kind brigade.

A thin facade over the scope group rules (services/scope_groups.py) and the unit helpers (services/units.py). A
brigade sits directly in a corps (ALLOWED_PARENT); moving it to another corps takes its posts along, like a group
move.
"""
from datetime import datetime, timezone
from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, Path, Query, Request, status
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.api.shared.helpers.db_errors import unique_violation_as
from app.backend.api.shared.helpers.scope_group_helper import audit_group, check_visible, get_unit, name_taken
from app.backend.api.v1.models.query.brigades import BrigadeFilterParams
from app.backend.api.v1.models.query.posts import PostFilterParams
from app.backend.api.v1.models.request.brigades import BrigadeCreate, BrigadeUpdate
from app.backend.api.v1.models.response.brigades import BrigadeResponse, brigade_to_response
from app.backend.api.v1.models.response.posts import PostResponse, posts_to_response
from app.backend.auth.access_control import AccessControlService
from app.backend.auth.context import AuthContext
from app.backend.auth.require import require
from app.backend.auth.scope import Node
from app.backend.database import get_session
from app.backend.enums.errors import AccessErrorCode, ErrorCode
from app.backend.enums.general import Permissions, ScopeGroupKind
from app.backend.enums.routes import BrigadeRoutes
from app.backend.errors.coded import CodedError
from app.backend.errors.openapi import GUARDED, problems
from app.backend.errors.problems import ApiError
from app.backend.filters.post_filter import PostFilterService
from app.backend.filters.unit_filter import UnitFilterService
from app.backend.models.post import Post
from app.backend.models.scope_group import ScopeGroup
from app.backend.services import scope_groups as groups_service
from app.backend.services.units import posts_within
from app.backend.utils.row_locks import lock, lock_first

router = APIRouter(prefix=BrigadeRoutes.BASE.value, tags=[BrigadeRoutes.tag()])

BRIGADE = ScopeGroupKind.BRIGADE

# Where a brigade may go: the corps is unknown, archived or outside scope_group.view, or not a corps
PLACEMENT_PROBLEMS = (ErrorCode.SCOPE_GROUP_NOT_FOUND, ErrorCode.SCOPE_GROUP_PARENT_NOT_ALLOWED)


async def _check_corps(session: AsyncSession, brigade_id: Optional[int], corps_id: int) -> None:
    """The destination is a live corps (404 otherwise) and a brigade may sit in it (409 otherwise)"""
    try:
        await groups_service.check_new_parent(session, brigade_id, corps_id)
        await groups_service.check_placement(session, BRIGADE, corps_id)
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
    caller holds scope_group.view. Archived ones only with include_deleted (requires scope_group.archive)."""
    AccessControlService.check_include_deleted(auth, filters, Permissions.SCOPE_GROUP_ARCHIVE)
    query = UnitFilterService.apply_filters(
        select(ScopeGroup), filters, BRIGADE, auth.groups_with(Permissions.SCOPE_GROUP_VIEW),
    )
    return [brigade_to_response(brigade, auth) for brigade in (await session.exec(query)).all()]


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
    # The corps is locked first: an archive of the corps waits, so no brigade appears inside an archived corps
    if await lock_first(session, select(ScopeGroup).where(ScopeGroup.id == data.corps_id)) is None:
        raise ApiError(ErrorCode.SCOPE_GROUP_NOT_FOUND)
    await _check_corps(session, None, data.corps_id)
    if await name_taken(session, data.name, data.corps_id):
        raise ApiError(ErrorCode.SCOPE_GROUP_NAME_TAKEN)

    brigade = ScopeGroup(name=data.name, description=data.description, parent_id=data.corps_id,
                         parent_key=data.corps_id, kind=BRIGADE.value)
    session.add(brigade)
    async with unique_violation_as(session, ErrorCode.SCOPE_GROUP_NAME_TAKEN):
        await session.commit()
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
    response_model=BrigadeResponse,
    responses=problems(
        *PLACEMENT_PROBLEMS, ErrorCode.SCOPE_GROUP_NAME_TAKEN, AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION, *GUARDED,
    ),
)
async def update_brigade(
        request: Request,
        brigade_id: Annotated[int, Path(ge=1)],
        data: BrigadeUpdate,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.SCOPE_GROUP_VIEW, Permissions.SCOPE_GROUP_UPDATE]))
):
    """Rename, describe or move a brigade; only the fields sent change. Archived, another kind of group, or outside
    the caller's scope_group.view: 404. A move (corps_id) takes every post of the brigade along: it also needs
    post.update covering the old and the new corps (403 permission_required_for_action, action move_brigade),
    answered before the existence of the new corps; the new corps unknown, archived or outside scope_group.view: 404,
    not a corps: 409 scope_group_parent_not_allowed. Nothing changes on a refusal."""
    brigade = await get_unit(session, auth, BRIGADE, brigade_id, lock=True)
    update_data = data.model_dump(exclude_unset=True)
    old_values = brigade.model_dump()

    new_corps = update_data.get("corps_id", brigade.parent_id)
    if new_corps != brigade.parent_id:
        check_visible(auth, new_corps)
        for corps_id in (brigade.parent_id, new_corps):
            AccessControlService.check_covers(auth, Permissions.POST_UPDATE, Node.group(corps_id), "move_brigade")
        # Groups before posts, the moved brigade first: the destination waits for nobody holding the brigade
        if await lock(session, select(ScopeGroup).where(ScopeGroup.id == new_corps)) is None:
            raise ApiError(ErrorCode.SCOPE_GROUP_NOT_FOUND)
        await _check_corps(session, brigade.id, new_corps)
    if ("corps_id" in update_data or "name" in update_data) and await name_taken(
            session, update_data.get("name", brigade.name), new_corps, exclude_id=brigade.id):
        raise ApiError(ErrorCode.SCOPE_GROUP_NAME_TAKEN)

    for key, value in update_data.items():
        if key != "corps_id":
            setattr(brigade, key, value)
    brigade.parent_id = new_corps
    brigade.parent_key = new_corps
    brigade.updated_at = datetime.now(timezone.utc)
    session.add(brigade)
    async with unique_violation_as(session, ErrorCode.SCOPE_GROUP_NAME_TAKEN):
        await session.commit()
    await session.refresh(brigade)

    await audit_group(request, session, auth, "update", brigade, old_values)
    await session.commit()
    return brigade_to_response(brigade, auth)


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
