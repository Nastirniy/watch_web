from datetime import datetime, timezone
from typing import Annotated, Dict, List, Optional

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.api.shared.helpers.audit_helper import client_info
from app.backend.api.shared.helpers.db_errors import unique_violation_as
from app.backend.api.v1.models.query.scope_groups import ScopeGroupFilterParams
from app.backend.api.v1.models.request.scope_groups import ScopeGroupCreate, ScopeGroupUpdate
from app.backend.api.v1.models.response.scope_groups import (
    ScopeGroupResponse, ScopeTreeGroup, ScopeTreePost, ScopeTreeResponse,
)
from app.backend.auth.access_control import AccessControlService
from app.backend.auth.context import AuthContext
from app.backend.auth.require import require
from app.backend.database import get_session
from app.backend.enums.errors import AccessErrorCode, ErrorCode
from app.backend.enums.general import Permissions
from app.backend.enums.routes import ScopeGroupRoutes
from app.backend.errors.coded import CodedError
from app.backend.errors.openapi import ADMIN, GUARDED, problems
from app.backend.errors.problems import ApiError
from app.backend.filters.scope_group_filter import ScopeGroupFilterService
from app.backend.models.post import Post
from app.backend.models.scope_group import ScopeGroup
from app.backend.services import scope_groups as groups_service
from app.backend.services.audit import create_audit_log

router = APIRouter(prefix=ScopeGroupRoutes.BASE.value, tags=[ScopeGroupRoutes.tag()])

# Every route of this module answers these: the tree rules raise them
TREE_PROBLEMS = (
    ErrorCode.SCOPE_GROUP_NOT_FOUND, ErrorCode.SCOPE_GROUP_CYCLE, ErrorCode.SCOPE_GROUP_DEPTH_EXCEEDED,
    ErrorCode.SCOPE_GROUP_NAME_TAKEN,
)


async def _name_taken(session: AsyncSession, name: str, parent_id: Optional[int], exclude_id: Optional[int] = None) -> bool:
    """Case-insensitive, archived groups included: the name is unique within its parent (parent_key 0 is the root)"""
    query = select(ScopeGroup.id).where(
        ScopeGroup.parent_key == (parent_id or 0), func.lower(ScopeGroup.name) == name.lower(),
    )
    if exclude_id is not None:
        query = query.where(ScopeGroup.id != exclude_id)
    return (await session.exec(query)).first() is not None


async def _group_or_404(session: AsyncSession, group_id: int, include_deleted: bool = False) -> ScopeGroup:
    try:
        return await groups_service.get_group(session, group_id, include_deleted)
    except CodedError as exc:
        raise ApiError.from_coded(exc)


async def _check_parent(session: AsyncSession, group_id: Optional[int], parent_id: Optional[int]) -> None:
    try:
        await groups_service.check_new_parent(session, group_id, parent_id)
    except CodedError as exc:
        raise ApiError.from_coded(exc)


async def _audit(request: Request, session: AsyncSession, auth: AuthContext, action: str, group: ScopeGroup,
                 old_values: Optional[dict]) -> None:
    await create_audit_log(
        session=session,
        action=action,
        resource_type="scope_group",
        resource_id=group.id,
        user_id=auth.user.id,
        old_values=old_values,
        new_values=group.model_dump(),
        details={"message": f"User {auth.user.username} {action}d scope group '{group.name}'"},
        **client_info(request, auth)
    )


@router.get(
    ScopeGroupRoutes.ROOT.value,
    response_model=List[ScopeGroupResponse],
    responses=problems(AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION, *ADMIN),
)
async def list_scope_groups(
        filters: Annotated[ScopeGroupFilterParams, Query()],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.SCOPE_GROUP_VIEW], admin=True))
):
    """List scope groups with filters. Archived ones only with include_deleted (requires scope_group.archive)."""
    AccessControlService.check_include_deleted(auth, filters, Permissions.SCOPE_GROUP_ARCHIVE)
    query = ScopeGroupFilterService.apply_filters(select(ScopeGroup), filters)
    result = await session.exec(query)
    return [ScopeGroupResponse.model_validate(g) for g in result.all()]


@router.post(
    ScopeGroupRoutes.ROOT.value,
    response_model=ScopeGroupResponse,
    responses=problems(*TREE_PROBLEMS, *ADMIN),
)
async def create_scope_group(
        request: Request,
        data: ScopeGroupCreate,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(
            all=[Permissions.SCOPE_GROUP_VIEW, Permissions.SCOPE_GROUP_CREATE], admin=True))
):
    """Create a group under parent_id (null: directly under the root). Names are unique within a parent; groups
    nest at most 6 levels deep."""
    await _check_parent(session, None, data.parent_id)
    if await _name_taken(session, data.name, data.parent_id):
        raise ApiError(ErrorCode.SCOPE_GROUP_NAME_TAKEN)

    group = ScopeGroup(
        name=data.name, parent_id=data.parent_id, parent_key=data.parent_id or 0, description=data.description,
    )
    session.add(group)
    async with unique_violation_as(session, ErrorCode.SCOPE_GROUP_NAME_TAKEN):
        await session.commit()
    await session.refresh(group)

    await _audit(request, session, auth, "create", group, None)
    await session.commit()
    return ScopeGroupResponse.model_validate(group)


@router.get(
    ScopeGroupRoutes.TREE.value,
    response_model=ScopeTreeResponse,
    responses=problems(AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION, *ADMIN),
)
async def get_scope_tree(
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.SCOPE_GROUP_VIEW], admin=True))
):
    """The whole tree below the root: groups with their subgroups and posts, then the posts directly under the
    root. Archived groups and posts are left out."""
    groups = (await session.exec(
        select(ScopeGroup).where(ScopeGroup.is_deleted == False).order_by(ScopeGroup.name, ScopeGroup.id)  # noqa: E712
    )).all()
    posts = (await session.exec(
        select(Post).where(Post.is_deleted == False).order_by(Post.name, Post.id)  # noqa: E712
    )).all()

    nodes: Dict[int, ScopeTreeGroup] = {
        g.id: ScopeTreeGroup(id=g.id, name=g.name, description=g.description, groups=[], posts=[]) for g in groups
    }
    top = ScopeTreeResponse(groups=[], posts=[])
    for g in groups:
        (nodes[g.parent_id].groups if g.parent_id in nodes else top.groups).append(nodes[g.id])
    for p in posts:
        item = ScopeTreePost(id=p.id, name=p.name, code=p.code)
        (nodes[p.group_id].posts if p.group_id in nodes else top.posts).append(item)
    return top


@router.get(
    ScopeGroupRoutes.GROUP_BY_ID.value,
    response_model=ScopeGroupResponse,
    responses=problems(ErrorCode.SCOPE_GROUP_NOT_FOUND, *ADMIN),
)
async def get_scope_group(
        group_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.SCOPE_GROUP_VIEW], admin=True))
):
    """Get a scope group by ID. An archived group answers 404."""
    return ScopeGroupResponse.model_validate(await _group_or_404(session, group_id))


@router.patch(
    ScopeGroupRoutes.GROUP_BY_ID.value,
    response_model=ScopeGroupResponse,
    responses=problems(*TREE_PROBLEMS, *ADMIN),
)
async def update_scope_group(
        request: Request,
        group_id: int,
        data: ScopeGroupUpdate,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(
            all=[Permissions.SCOPE_GROUP_VIEW, Permissions.SCOPE_GROUP_UPDATE], admin=True))
):
    """Rename, describe or move a group (parent_id null: directly under the root). Only the fields sent change.
    A group cannot move into its own subtree, and the moved subtree must still fit 6 levels. Archived: 404."""
    group = await _group_or_404(session, group_id)
    update_data = data.model_dump(exclude_unset=True)
    old_values = group.model_dump()

    new_parent = update_data.get("parent_id", group.parent_id)
    new_name = update_data.get("name", group.name)
    if "parent_id" in update_data and new_parent != group.parent_id:
        await _check_parent(session, group.id, new_parent)
    if ("parent_id" in update_data or "name" in update_data) and await _name_taken(
            session, new_name, new_parent, exclude_id=group.id):
        raise ApiError(ErrorCode.SCOPE_GROUP_NAME_TAKEN)

    for key, value in update_data.items():
        setattr(group, key, value)
    group.parent_key = group.parent_id or 0
    group.updated_at = datetime.now(timezone.utc)
    session.add(group)
    async with unique_violation_as(session, ErrorCode.SCOPE_GROUP_NAME_TAKEN):
        await session.commit()
    await session.refresh(group)

    await _audit(request, session, auth, "update", group, old_values)
    await session.commit()
    return ScopeGroupResponse.model_validate(group)


@router.post(
    ScopeGroupRoutes.GROUP_ARCHIVE.value,
    response_model=ScopeGroupResponse,
    responses=problems(ErrorCode.SCOPE_GROUP_NOT_FOUND, ErrorCode.SCOPE_GROUP_NOT_EMPTY, *ADMIN),
)
async def archive_scope_group(
        request: Request,
        group_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(
            all=[Permissions.SCOPE_GROUP_VIEW, Permissions.SCOPE_GROUP_ARCHIVE], admin=True))
):
    """Archive a group (is_deleted=true). Only an empty group: no active subgroup and no active post (409
    scope_group_not_empty). Already archived or unknown: 404. Undo with POST /{group_id}/restore."""
    group = await _group_or_404(session, group_id)
    try:
        await groups_service.check_empty(session, group.id)
    except CodedError as exc:
        raise ApiError.from_coded(exc)

    old_values = group.model_dump()
    group.is_deleted = True
    group.updated_at = datetime.now(timezone.utc)
    session.add(group)
    await _audit(request, session, auth, "archive", group, old_values)
    await session.commit()
    await session.refresh(group)
    return ScopeGroupResponse.model_validate(group)


@router.post(
    ScopeGroupRoutes.GROUP_RESTORE.value,
    response_model=ScopeGroupResponse,
    responses=problems(ErrorCode.SCOPE_GROUP_NOT_ARCHIVED, *TREE_PROBLEMS, *ADMIN),
)
async def restore_scope_group(
        request: Request,
        group_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(
            all=[Permissions.SCOPE_GROUP_VIEW, Permissions.SCOPE_GROUP_ARCHIVE], admin=True))
):
    """Restore an archived group (is_deleted=false) under its former parent. Unknown: 404, and 404 when the
    parent is archived (restore the parent first). Not archived: 409."""
    group = await _group_or_404(session, group_id, include_deleted=True)
    if not group.is_deleted:
        raise ApiError(ErrorCode.SCOPE_GROUP_NOT_ARCHIVED)
    await _check_parent(session, group.id, group.parent_id)
    old_values = group.model_dump()
    group.is_deleted = False
    group.updated_at = datetime.now(timezone.utc)
    session.add(group)
    await _audit(request, session, auth, "restore", group, old_values)
    async with unique_violation_as(session, ErrorCode.SCOPE_GROUP_NAME_TAKEN):
        await session.commit()
    await session.refresh(group)
    return ScopeGroupResponse.model_validate(group)
