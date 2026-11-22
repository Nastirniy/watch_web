from datetime import datetime, timezone
from typing import Annotated, Dict, List, Optional

from fastapi import APIRouter, Depends, Path, Query, Request
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.api.shared.helpers.audit_helper import client_info
from app.backend.api.shared.helpers.db_errors import unique_violation_as
from app.backend.api.shared.helpers.move_helper import admit_move, changed_fields, move_conflict, move_step
from app.backend.api.shared.helpers.scope_group_helper import (
    audit_group, check_visible, lock_groups, lock_unit, name_taken, parent_location, parent_node,
)
from app.backend.api.v1.models.query.scope_groups import ScopeGroupFilterParams
from app.backend.api.v1.models.request.scope_groups import ScopeGroupCreate, ScopeGroupUpdate
from app.backend.api.v1.models.response.moves import MOVE_ACCEPTED, move_accepted
from app.backend.api.v1.models.response.scope_groups import (
    ScopeGroupResponse, ScopeGroupUpdateResponse, ScopeTreeGroup, ScopeTreePost, ScopeTreeResponse,
)
from app.backend.auth.access_control import AccessControlService
from app.backend.api.versioning import PublishedVersion, request_version
from app.backend.auth.context import AuthContext
from app.backend.auth.move_access import move_requirements
from app.backend.auth.privileges import check_move, check_node_restore
from app.backend.auth.require import require
from app.backend.auth.scope import Node, ROOT
from app.backend.database import get_session
from app.backend.enums.errors import AccessErrorCode, ErrorCode
from app.backend.enums.general import MoveCancelCause, MoveNodeKind, Permissions, ScopeGroupKind
from app.backend.enums.routes import ScopeGroupRoutes
from app.backend.errors.coded import CodedError
from app.backend.errors.openapi import GUARDED, problems
from app.backend.errors.problems import ApiError
from app.backend.filters.scope_group_filter import ScopeGroupFilterService
from app.backend.models.post import Post
from app.backend.models.scope_group import ScopeGroup
from app.backend.services import move_queue, moves
from app.backend.services import scope_groups as groups_service
from app.backend.services.events import publish_scope_changed

router = APIRouter(prefix=ScopeGroupRoutes.BASE.value, tags=[ScopeGroupRoutes.tag()])

# Every route of this module answers these: the tree rules raise them
TREE_PROBLEMS = (
    ErrorCode.SCOPE_GROUP_NOT_FOUND, ErrorCode.SCOPE_GROUP_CYCLE, ErrorCode.SCOPE_GROUP_DEPTH_EXCEEDED,
    ErrorCode.SCOPE_GROUP_NAME_TAKEN, ErrorCode.SCOPE_GROUP_PARENT_NOT_ALLOWED,
)


async def _group_or_404(session: AsyncSession, auth: AuthContext, group_id: int, include_deleted: bool = False,
                        lock: bool = False) -> ScopeGroup:
    check_visible(auth, group_id)
    try:
        return await groups_service.get_group(session, group_id, include_deleted, lock=lock)
    except CodedError as exc:
        raise ApiError.from_coded(exc)


async def _check_parent(session: AsyncSession, group_id: Optional[int], parent_id: Optional[int],
                        kind: ScopeGroupKind) -> None:
    """The tree rules, then where a group of this kind may sit (409 scope_group_parent_not_allowed)"""
    try:
        await groups_service.check_new_parent(session, group_id, parent_id)
        await groups_service.check_placement(session, kind, parent_id)
    except CodedError as exc:
        raise ApiError.from_coded(exc)


def _check_parent_rights(auth: AuthContext, group_id: Optional[int], parent_id: Optional[int],
                         old_parent_id: Optional[int] = None) -> None:
    """A parent the caller names (create, move): outside the caller's scope_group.view it answers 404 like an
    unknown one; the root (None) is no id to hide: without scope_group.view there it answers 403
    permission_required_for_action (create_scope_group / move_scope_group). A create needs scope_group.create covering
    the parent location (403 permission_required_for_action, create_scope_group). A move (group_id) needs
    scope_group.view and scope_group.update covering the old and the new parent location and, as it takes every
    post below the group along, post.update and post.view covering both, as a post move (403 permission_required_for_action,
    move_scope_group). All before the tree rules (the parent exists and is live, no cycle, the depth), the placement
    rules of the kind and the caller's name check."""
    action = "create_scope_group" if group_id is None else "move_scope_group"
    if parent_id is not None:
        check_visible(auth, parent_id)
    else:
        AccessControlService.check_covers(auth, Permissions.SCOPE_GROUP_VIEW, ROOT, action)
    if group_id is None:
        AccessControlService.check_covers(auth, Permissions.SCOPE_GROUP_CREATE, parent_location(parent_id), action)
    else:
        # X.view AND X.update at both ends: the new parent is seen by check_visible (the root above), the old one here
        for permission, location in move_requirements(MoveNodeKind.GROUP, parent_location(old_parent_id),
                                                      parent_location(parent_id)):
            AccessControlService.check_covers(auth, permission, location, action)


@router.get(
    ScopeGroupRoutes.ROOT.value,
    response_model=List[ScopeGroupResponse],
    responses=problems(AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION, *GUARDED),
)
async def list_scope_groups(
        filters: Annotated[ScopeGroupFilterParams, Query()],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.SCOPE_GROUP_VIEW]))
):
    """List scope groups with filters: the groups inside the subtrees where the caller holds scope_group.view (every
    group with it at the root). Archived ones only with include_deleted (requires scope_group.archive), and only
    where scope_group.archive is in effect."""
    archived = AccessControlService.archived_group_scope(auth, filters, Permissions.SCOPE_GROUP_ARCHIVE)
    query = ScopeGroupFilterService.apply_filters(
        select(ScopeGroup), filters, auth.groups_with(Permissions.SCOPE_GROUP_VIEW), archived_ids=archived,
    )
    result = await session.exec(query)
    return [ScopeGroupResponse.model_validate(g) for g in result.all()]


@router.post(
    ScopeGroupRoutes.ROOT.value,
    response_model=ScopeGroupResponse,
    responses=problems(*TREE_PROBLEMS, AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION, *GUARDED),
)
async def create_scope_group(
        request: Request,
        data: ScopeGroupCreate,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(
            all=[Permissions.SCOPE_GROUP_VIEW, Permissions.SCOPE_GROUP_CREATE]))
):
    """Create a group under parent_id (null: directly under the root). Names are unique within a parent; groups
    nest at most 6 levels deep. A parent outside the caller's scope_group.view answers 404 like an unknown one;
    scope_group.create must cover the parent (the root for null, where scope_group.view is needed too), otherwise 403
    permission_required_for_action (action create_scope_group), before the tree rules and the name check. Creates a plain group: it sits under the root or a plain group, never in a corps or brigade (409
    scope_group_parent_not_allowed)."""
    _check_parent_rights(auth, None, data.parent_id)
    # The parent is locked first, as a post and a brigade do: an archive or a move of it waits, and the insert never
    # meets a parent changed after the snapshot (MariaDB error 1020), nor holds its new row while it waits for the parent
    # (error 1213 with a move that locks what lies below that parent)
    await lock_groups(session, auth.tree, [data.parent_id])
    await _check_parent(session, None, data.parent_id, ScopeGroupKind.GROUP)
    if await name_taken(session, data.name, data.parent_id):
        raise ApiError(ErrorCode.SCOPE_GROUP_NAME_TAKEN)

    group = ScopeGroup(
        name=data.name, parent_id=data.parent_id, parent_key=data.parent_id or 0, description=data.description,
    )
    session.add(group)
    # The audit row joins the change: one commit (the name index is hit at the flush)
    async with unique_violation_as(session, ErrorCode.SCOPE_GROUP_NAME_TAKEN):
        await session.flush()
        await session.refresh(group)
        await audit_group(request, session, auth, "create", group, None)
        publish_scope_changed(session, (parent_location(group.parent_id),))      # at the parent (posts.create_post)
        await session.commit()
    return ScopeGroupResponse.model_validate(group)


@router.get(
    ScopeGroupRoutes.TREE.value,
    response_model=ScopeTreeResponse,
    responses=problems(AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION, *GUARDED),
)
async def get_scope_tree(
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.SCOPE_GROUP_VIEW]))
):
    """The part of the scope tree the caller sees, for the scope picker: the subtrees where the caller holds
    scope_group.view, with the posts where post.view is in effect. The top level holds the topmost nodes the caller
    sees: with scope_group.view at the root the groups and the posts directly under the root; otherwise the topmost
    groups (and posts) where it is held. Archived groups and posts are left out, and so is everything below an
    archived group (a post restored while its group is archived keeps its group_id: it is not shown as a post of
    the root)."""
    tree = auth.tree
    viewable_posts = auth.posts_with(Permissions.POST_VIEW)

    def shown(node: Node) -> bool:
        """Inside a subtree where scope_group.view is held, below no archived group"""
        return auth.covers(Permissions.SCOPE_GROUP_VIEW, node) and not any(
            tree.is_archived(above) for above in tree.ancestors(node)[1:])

    groups = [g for g in (await session.exec(
        select(ScopeGroup).where(ScopeGroup.is_deleted == False).order_by(ScopeGroup.name, ScopeGroup.id)  # noqa: E712
    )).all() if shown(Node.group(g.id))]
    posts = [p for p in (await session.exec(
        select(Post).where(Post.is_deleted == False).order_by(Post.name, Post.id)  # noqa: E712
    )).all() if (viewable_posts is None or p.id in viewable_posts) and shown(Node.post(p.id))]

    nodes: Dict[int, ScopeTreeGroup] = {
        g.id: ScopeTreeGroup(id=g.id, name=g.name, kind=g.kind, description=g.description, groups=[], posts=[])
        for g in groups
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
    responses=problems(ErrorCode.SCOPE_GROUP_NOT_FOUND, *GUARDED),
)
async def get_scope_group(
        group_id: Annotated[int, Path(ge=1)],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.SCOPE_GROUP_VIEW]))
):
    """Get a scope group by ID. An archived group, and a group outside the subtrees where the caller holds
    scope_group.view, answer 404 like an unknown one."""
    return ScopeGroupResponse.model_validate(await _group_or_404(session, auth, group_id))


@router.patch(
    ScopeGroupRoutes.GROUP_BY_ID.value,
    response_model=ScopeGroupUpdateResponse,
    responses={**MOVE_ACCEPTED, **problems(
        *TREE_PROBLEMS, AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION, AccessErrorCode.GRANT_EXCEEDS_OWN_PERMISSIONS,
        ErrorCode.MOVE_PENDING, ErrorCode.MOVE_CONFLICT, ErrorCode.MOVE_NOT_ALONE, *GUARDED,
    )},
)
async def update_scope_group(
        request: Request,
        group_id: Annotated[int, Path(ge=1)],
        data: ScopeGroupUpdate,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(
            all=[Permissions.SCOPE_GROUP_VIEW, Permissions.SCOPE_GROUP_UPDATE])),
        version: PublishedVersion = Depends(request_version),
):
    """Rename, describe or move a group (parent_id null: directly under the root). Only the fields sent change.
    A group cannot move into its own subtree, and the moved subtree must still fit 6 levels. Archived, or outside
    the caller's scope_group.view (the group or the new parent): 404. scope_group.update must cover the parent of the
    group (the root for a group directly under it), otherwise 403 permission_required_for_action (action
    update_scope_group): a holder at a group manages the groups inside it, not the group itself. A new name without a
    move also needs scope_group.view covering that parent (same answer), the name is checked against it. A move needs
    scope_group.view and scope_group.update covering the old and the new parent location (the root for a group
    directly under it) and, as it takes every post below the group along, post.update and post.view covering both (the group or above it; the root for a group directly under it),
    otherwise 403 permission_required_for_action (action move_scope_group), answered before the existence of the new
    parent and the tree rules; nothing changes on a refusal. Every role assignment at the new parent and above it that
    does not already apply at the group comes into effect for the whole subtree: the caller must be able to grant each
    of them at the group, holding every permission of its role there before the move, the grants of other users
    included (403 grant_exceeds_own_permissions, after the tree rules). The group keeps its kind and
    the parent must suit it (a plain group never moves into a corps or brigade, a corps stays under the root, a
    brigade in a corps): 409 scope_group_parent_not_allowed.

    A move states where it thinks the node is: with parent_id send expected_parent_id, the value the client read (null: directly under the root; presence counts).
    Checked under the move lock, force or not, in this order. The group already at parent_id: 200 with already_there true,
    nothing written, the other fields sent included, no audit row. expected_parent_id missing or other than where
    the group sits: 409 move_conflict with the current place (current_place root; group with current_group_id; hidden for a
    group outside the caller's scope_group.view) and nothing moves: read the record and decide again. So a client that
    sends the whole record from an old form is refused, not obeyed. parent_id sent with the current value is no move: an
    ordinary edit, with no expected_parent_id or with the same value there.

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
    update_data = data.model_dump(exclude_unset=True, exclude={"force", "expected_parent_id"})
    # A request that changes the parent takes the locks of a move, everything below the group included
    # (services/moves.py). The current value sent back, or a target inside the group, takes none: the row decides
    move_locks = None
    if "parent_id" in update_data and moves.takes_move_locks(auth.tree, Node.group(group_id), update_data["parent_id"]):
        check_visible(auth, group_id)
        move_locks = await moves.lock_for_move(session, auth.tree, Node.group(group_id), update_data["parent_id"])
    # Groups top down: the old and the new parent, then the group (a unit archive takes the same order); a target
    # without the locks of a move may lie below the group, and is left alone
    group = await lock_unit(session, auth, None, group_id, also=[update_data.get("parent_id") if move_locks else None],
                            started=move_locks is not None)
    old_values = group.model_dump()

    new_parent = update_data.get("parent_id", group.parent_id)
    new_name = update_data.get("name", group.name)
    step = move_step(data, "parent_id", "expected_parent_id", group.parent_id)
    is_move = step is moves.MoveStep.MOVE
    if is_move:
        _check_parent_rights(auth, group.id, new_parent, old_parent_id=group.parent_id)
        await _check_parent(session, group.id, new_parent, ScopeGroupKind(group.kind))
        # What the new parent puts in effect at the subtree, read after lock_unit's locks (auth/privileges.py)
        await check_move(session, auth, Node.group(group.id), parent_location(new_parent))
    else:
        AccessControlService.check_covers(auth, Permissions.SCOPE_GROUP_UPDATE, parent_node(group), "update_scope_group")
        if step is moves.MoveStep.CONFLICT:
            raise move_conflict(auth, group.parent_id)
        if step is moves.MoveStep.ALREADY_THERE:
            return ScopeGroupUpdateResponse.model_validate(group).model_copy(update={"already_there": True})
        if "name" in update_data:
            # The name is checked against the names beside the group (409): view at that parent as well
            AccessControlService.check_covers(auth, Permissions.SCOPE_GROUP_VIEW, parent_node(group),
                                              "update_scope_group")
    if ("parent_id" in update_data or "name" in update_data) and await name_taken(
            session, new_name, new_parent, exclude_id=group.id):
        raise ApiError(ErrorCode.SCOPE_GROUP_NAME_TAKEN)
    if is_move:
        # Last of the checks: a move that has to wait commits its row here, nothing else changed
        waiting = await admit_move(request, session, auth, Node.group(group.id), group.parent_id, new_parent, move_locks,
                                   data.force, changed_fields(group, update_data, "parent_id"))
        if waiting is not None:
            return move_accepted(await moves.view_of(session, waiting, auth.tree), version)

    for key, value in update_data.items():
        setattr(group, key, value)
    group.parent_key = group.parent_id or 0
    group.updated_at = datetime.now(timezone.utc)
    session.add(group)
    # The audit row joins the change: one commit (the name index is hit at the flush)
    async with unique_violation_as(session, ErrorCode.SCOPE_GROUP_NAME_TAKEN):
        await session.flush()
        await session.refresh(group)
        await audit_group(request, session, auth, "update", group, old_values)
        if is_move:     # an immediate move: to the new place and the old one
            publish_scope_changed(session, (Node.group(group.id), parent_location(old_values["parent_id"])))
        await session.commit()
    return ScopeGroupResponse.model_validate(group)


@router.post(
    ScopeGroupRoutes.GROUP_ARCHIVE.value,
    response_model=ScopeGroupResponse,
    responses=problems(
        ErrorCode.SCOPE_GROUP_NOT_FOUND, ErrorCode.SCOPE_GROUP_NOT_EMPTY, AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION,
        *GUARDED,
    ),
)
async def archive_scope_group(
        request: Request,
        group_id: Annotated[int, Path(ge=1)],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(
            all=[Permissions.SCOPE_GROUP_VIEW, Permissions.SCOPE_GROUP_ARCHIVE]))
):
    """Archive a group (is_deleted=true). Only an empty group: no active subgroup and no active post (409
    scope_group_not_empty). Already archived, unknown or outside the caller's scope_group.view: 404. scope_group.archive
    must cover its parent (the root for a group directly under it), otherwise 403 permission_required_for_action
    (action archive_scope_group). A waiting move of the group is cancelled. Undo with POST /{group_id}/restore."""
    group = await _group_or_404(session, auth, group_id, lock=True)
    AccessControlService.check_covers(auth, Permissions.SCOPE_GROUP_ARCHIVE, parent_node(group), "archive_scope_group")
    # Before the first plain read: a cancel or a run of the move waits for the archive
    waiting_moves = await move_queue.lock_waiting(session, [Node.group(group.id)])
    try:
        await groups_service.check_empty(session, group.id)
    except CodedError as exc:
        raise ApiError.from_coded(exc)

    old_values = group.model_dump()
    group.is_deleted = True
    group.updated_at = datetime.now(timezone.utc)
    session.add(group)
    await audit_group(request, session, auth, "archive", group, old_values)
    await moves.cancel_locked(session, waiting_moves, MoveCancelCause.NODE_ARCHIVED, auth.user.id,
                              client_info(request, auth))
    publish_scope_changed(session, (Node.group(group.id),))
    await session.commit()
    await session.refresh(group)
    return ScopeGroupResponse.model_validate(group)


@router.post(
    ScopeGroupRoutes.GROUP_RESTORE.value,
    response_model=ScopeGroupResponse,
    responses=problems(
        ErrorCode.SCOPE_GROUP_NOT_ARCHIVED, *TREE_PROBLEMS, AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION,
        AccessErrorCode.GRANT_EXCEEDS_OWN_PERMISSIONS, *GUARDED,
    ),
)
async def restore_scope_group(
        request: Request,
        group_id: Annotated[int, Path(ge=1)],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(
            all=[Permissions.SCOPE_GROUP_VIEW, Permissions.SCOPE_GROUP_ARCHIVE]))
):
    """Restore an archived group (is_deleted=false) under its former parent. Unknown: 404, and 404 when the
    parent is archived (restore the parent first); outside the caller's scope_group.view: 404. scope_group.archive
    must cover its parent (the root for a group directly under it), otherwise 403 permission_required_for_action
    (action restore_scope_group). Not archived: 409. The role assignments made at the group apply again: the caller
    must hold every permission of each of their roles at the group (403 grant_exceeds_own_permissions), as for a
    role's restore."""
    group = await lock_unit(session, auth, None, group_id, include_deleted=True)   # its parent first
    AccessControlService.check_covers(auth, Permissions.SCOPE_GROUP_ARCHIVE, parent_node(group), "restore_scope_group")
    if not group.is_deleted:
        raise ApiError(ErrorCode.SCOPE_GROUP_NOT_ARCHIVED)
    await _check_parent(session, group.id, group.parent_id, ScopeGroupKind(group.kind))
    await check_node_restore(session, auth, Node.group(group.id))
    old_values = group.model_dump()
    group.is_deleted = False
    group.updated_at = datetime.now(timezone.utc)
    session.add(group)
    await audit_group(request, session, auth, "restore", group, old_values)
    publish_scope_changed(session, (Node.group(group.id),))
    async with unique_violation_as(session, ErrorCode.SCOPE_GROUP_NAME_TAKEN):
        await session.commit()
    await session.refresh(group)
    return ScopeGroupResponse.model_validate(group)
