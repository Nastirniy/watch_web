"""Corps: the top units of the organisation, scope groups of kind corps directly under the root.

A thin facade over the scope group rules (services/scope_groups.py) and the unit helpers (services/units.py): a corps
is a scope group, so roles given at it cover its brigades and posts, and /scope-groups shows it too (kind corps).
"""
from dataclasses import replace
from datetime import datetime, timezone
from typing import Annotated, List

from fastapi import APIRouter, Depends, Path, Query, Request, status
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.api.shared.helpers.db_errors import unique_violation_as
from app.backend.api.shared.helpers.scope_group_helper import audit_group, get_unit, name_taken
from app.backend.api.v1.models.query.corps import UnitFilterParams
from app.backend.api.v1.models.query.posts import PostFilterParams
from app.backend.api.v1.models.request.corps import CorpsCreate, CorpsUpdate
from app.backend.api.v1.models.response.brigades import BrigadeResponse, brigade_to_response
from app.backend.api.v1.models.response.corps import CorpsResponse, corps_to_response
from app.backend.api.v1.models.response.posts import PostResponse, posts_to_response
from app.backend.auth.access_control import AccessControlService
from app.backend.auth.context import AuthContext
from app.backend.auth.require import require
from app.backend.auth.scope import ROOT
from app.backend.database import get_session
from app.backend.enums.errors import AccessErrorCode, ErrorCode
from app.backend.enums.general import Permissions, ScopeGroupKind
from app.backend.enums.routes import CorpsRoutes
from app.backend.errors.openapi import GUARDED, problems
from app.backend.errors.problems import ApiError
from app.backend.filters.post_filter import PostFilterService
from app.backend.filters.unit_filter import UnitFilterService
from app.backend.models.post import Post
from app.backend.models.scope_group import ScopeGroup
from app.backend.services.units import posts_within

router = APIRouter(prefix=CorpsRoutes.BASE.value, tags=[CorpsRoutes.tag()])

CORPS = ScopeGroupKind.CORPS


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
    with it at the root). Archived ones only with include_deleted (requires scope_group.archive)."""
    AccessControlService.check_include_deleted(auth, filters, Permissions.SCOPE_GROUP_ARCHIVE)
    query = UnitFilterService.apply_filters(
        select(ScopeGroup), filters, CORPS, auth.groups_with(Permissions.SCOPE_GROUP_VIEW),
    )
    return [corps_to_response(corps, auth) for corps in (await session.exec(query)).all()]


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
    """Create a corps directly under the root: scope_group.create at the root (403 permission_required_for_action
    otherwise). Names are unique among the corps and the plain groups of the root, case-insensitive."""
    AccessControlService.check_covers(auth, Permissions.SCOPE_GROUP_CREATE, ROOT, "create_corps")
    if await name_taken(session, data.name, None):
        raise ApiError(ErrorCode.SCOPE_GROUP_NAME_TAKEN)

    corps = ScopeGroup(name=data.name, description=data.description, parent_id=None, parent_key=0, kind=CORPS.value)
    session.add(corps)
    async with unique_violation_as(session, ErrorCode.SCOPE_GROUP_NAME_TAKEN):
        await session.commit()
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
    responses=problems(ErrorCode.SCOPE_GROUP_NOT_FOUND, ErrorCode.SCOPE_GROUP_NAME_TAKEN, *GUARDED),
)
async def update_corps(
        request: Request,
        corps_id: Annotated[int, Path(ge=1)],
        data: CorpsUpdate,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.SCOPE_GROUP_VIEW, Permissions.SCOPE_GROUP_UPDATE]))
):
    """Rename or describe a corps; only the fields sent change. Archived, another kind of group, or outside the
    caller's scope_group.view: 404."""
    corps = await get_unit(session, auth, CORPS, corps_id, lock=True)
    update_data = data.model_dump(exclude_unset=True)
    old_values = corps.model_dump()
    if "name" in update_data and await name_taken(session, update_data["name"], None, exclude_id=corps.id):
        raise ApiError(ErrorCode.SCOPE_GROUP_NAME_TAKEN)

    for key, value in update_data.items():
        setattr(corps, key, value)
    corps.updated_at = datetime.now(timezone.utc)
    session.add(corps)
    async with unique_violation_as(session, ErrorCode.SCOPE_GROUP_NAME_TAKEN):
        await session.commit()
    await session.refresh(corps)

    await audit_group(request, session, auth, "update", corps, old_values)
    await session.commit()
    return corps_to_response(corps, auth)


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
    include_deleted (requires scope_group.archive). The corps: 404 as on GET /corps/{corps_id}."""
    AccessControlService.check_include_deleted(auth, filters, Permissions.SCOPE_GROUP_ARCHIVE)
    corps = await get_unit(session, auth, CORPS, corps_id)
    criteria = replace(UnitFilterService.criteria(filters), corps_id=corps.id)
    query = UnitFilterService.apply_filters(
        select(ScopeGroup), criteria, ScopeGroupKind.BRIGADE, auth.groups_with(Permissions.SCOPE_GROUP_VIEW),
    )
    return [brigade_to_response(brigade, auth) for brigade in (await session.exec(query)).all()]


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
