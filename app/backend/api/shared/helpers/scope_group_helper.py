"""Scope groups of every kind (plain groups, corps, brigades): what /scope-groups, /corps and /brigades share.

Visibility before existence: a group outside every subtree where the caller holds scope_group.view answers like an
unknown id. Names are unique within a parent, case-insensitive, archived groups included (parent_key 0 is the root,
shared by root plain groups and corps).

Lock order (MariaDB snapshot isolation, utils/row_locks.py): every write that changes where groups or posts sit locks
groups top down (a corps before its brigades; by depth in the tree, then by id), then posts in id order. The database
does not choose an order: MariaDB takes the locks in the order of the statements, and this code fixes that order. The
rule "no active post or brigade inside an archived unit" spans rows and holds only because every such writer takes the
same locks in the same order.
"""
from datetime import datetime, timezone
from typing import Iterable, Optional

from fastapi import Request
from sqlalchemy import and_, func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.api.shared.helpers.audit_helper import client_info
from app.backend.auth.access_control import AccessControlService
from app.backend.auth.context import AuthContext
from app.backend.auth.scope import Node, ROOT, ScopeTree
from app.backend.enums.audit import AuditEvent
from app.backend.enums.errors import ErrorCode
from app.backend.enums.general import Permissions, ScopeGroupKind, StatsGroupBy, UnitPlacement
from app.backend.errors.coded import CodedError
from app.backend.errors.problems import ApiError
from app.backend.models.observation import Observation
from app.backend.models.scope_group import ScopeGroup
from app.backend.services import scope_groups as groups_service
from app.backend.services.audit import create_audit_log
from app.backend.services.observation_counts import ObservationStats, observation_stats
from app.backend.services.units import observation_unit_condition, recorded_in, unit_post_ids
from app.backend.utils.row_locks import lock, lock_first


async def name_taken(session: AsyncSession, name: str, parent_id: Optional[int], exclude_id: Optional[int] = None) -> bool:
    """Case-insensitive, archived groups included: the name is unique within its parent (parent_key 0 is the root)"""
    query = select(ScopeGroup.id).where(
        ScopeGroup.parent_key == (parent_id or 0), func.lower(ScopeGroup.name) == name.lower(),
    )
    if exclude_id is not None:
        query = query.where(ScopeGroup.id != exclude_id)
    return (await session.exec(query)).first() is not None


def parent_location(parent_id: Optional[int]) -> Node:
    """The node of a parent: that group, the root for None (a group directly under the root)"""
    return Node.group(parent_id) if parent_id is not None else ROOT


def parent_node(group: ScopeGroup) -> Node:
    """Where the group sits: its parent group, the root for a group directly under it. A write on an existing group
    needs its scope_group permission covering this node: a holder at a group manages the groups inside it, never the
    group itself."""
    return parent_location(group.parent_id)


def check_visible(auth: AuthContext, group_id: int) -> None:
    """Scope before existence: a group outside every subtree where the caller holds scope_group.view answers like an
    unknown id (an outsider learns nothing about ids)"""
    if not auth.covers(Permissions.SCOPE_GROUP_VIEW, Node.group(group_id)):
        raise ApiError(ErrorCode.SCOPE_GROUP_NOT_FOUND)


async def get_unit(session: AsyncSession, auth: AuthContext, kind: ScopeGroupKind, unit_id: int,
                   include_deleted: bool = False, lock: bool = False) -> ScopeGroup:
    """The group of this kind; 404 scope_group_not_found when it is unknown, archived (unless include_deleted),
    outside the caller's scope_group.view or of another kind (a brigade id on /corps/{id}). lock=True locks it as
    the first statement of the write (utils/row_locks.py)."""
    check_visible(auth, unit_id)
    query = select(ScopeGroup).where(ScopeGroup.id == unit_id, ScopeGroup.kind == ScopeGroupKind(kind).value)
    if not include_deleted:
        query = query.where(ScopeGroup.is_deleted == False)  # noqa: E712
    group = await lock_first(session, query) if lock else (await session.exec(query)).first()
    if group is None:
        raise ApiError(ErrorCode.SCOPE_GROUP_NOT_FOUND)
    return group


async def audit_group(request: Request, session: AsyncSession, auth: AuthContext, action: str, group: ScopeGroup,
                      old_values: Optional[dict]) -> None:
    """One audit row (resource_type scope_group) for a change of a group of any kind"""
    await create_audit_log(
        session, AuditEvent(f"scope_group.{action}"), scope=Node.group(group.id), resource_id=group.id,
        params={"group_name": group.name, "group_kind": group.kind},
        user_id=auth.user.id,
        old_values=old_values,
        new_values=group.model_dump(),
        **client_info(request, auth)
    )


def _top_down(tree: ScopeTree, group_id: int) -> tuple:
    """Sort key of the lock order: depth in the tree, then id"""
    return len(tree.ancestors(Node.group(group_id))), group_id


async def lock_groups(session: AsyncSession, tree: ScopeTree, group_ids: Iterable[Optional[int]]) -> bool:
    """Lock these groups top down (the first with lock_first); False when there is none (the write has not begun).
    Only groups the request's tree knows: a lock on a row that may not exist would take a gap lock that holds up
    concurrent creates (the caller's own existence check answers 404 for an unknown id)."""
    ordered = sorted({group_id for group_id in group_ids if group_id in tree.group_parent},
                     key=lambda g: _top_down(tree, g))
    for position, group_id in enumerate(ordered):
        await (lock if position else lock_first)(session, select(ScopeGroup).where(ScopeGroup.id == group_id))
    return bool(ordered)


async def lock_unit(session: AsyncSession, auth: AuthContext, kind: Optional[ScopeGroupKind], unit_id: int,
                    include_deleted: bool = False, also: Iterable[Optional[int]] = ()) -> ScopeGroup:
    """get_unit for a write that may change what sits in the group or where it sits: its parent (from the request's
    tree) and the groups in also (the destination of a move) are locked first, then the group (groups top down). A
    group moved since the tree was read gets its new parent locked too. kind None: a group of any kind."""
    check_visible(auth, unit_id)
    parent_id = auth.tree.group_parent.get(unit_id)
    query = select(ScopeGroup).where(ScopeGroup.id == unit_id)
    if kind is not None:
        query = query.where(ScopeGroup.kind == ScopeGroupKind(kind).value)
    if not include_deleted:
        query = query.where(ScopeGroup.is_deleted == False)  # noqa: E712
    started = await lock_groups(session, auth.tree, [parent_id, *also])
    unit = await (lock if started else lock_first)(session, query)
    if unit is None:
        raise ApiError(ErrorCode.SCOPE_GROUP_NOT_FOUND)
    if unit.parent_id is not None and unit.parent_id != parent_id:
        await lock(session, select(ScopeGroup).where(ScopeGroup.id == unit.parent_id))
    return unit


def _check_detach(auth: AuthContext, unit: ScopeGroup, action: str) -> None:
    """A detach moves the posts of a unit to its parent: post.update and post.view cover the unit and the parent (403
    permission_required_for_action)"""
    for location in (Node.group(unit.id), parent_node(unit)):
        for permission in (Permissions.POST_UPDATE, Permissions.POST_VIEW):
            AccessControlService.check_covers(auth, permission, location, action)


def _check_cascade(auth: AuthContext, unit: ScopeGroup, action: str) -> None:
    """A cascade archives every active post below the unit: post.archive and post.view cover the unit (403
    permission_required_for_action)"""
    for permission in (Permissions.POST_ARCHIVE, Permissions.POST_VIEW):
        AccessControlService.check_covers(auth, permission, Node.group(unit.id), action)


def _posts_changed(auth: AuthContext, unit: ScopeGroup, cascade: bool) -> frozenset:
    """The active posts the request's tree shows the archive would change: with cascade every one below the unit; a
    detach moves only the posts directly in the unit (those of its brigades never move: a corps with an active
    brigade answers corps_has_brigades)"""
    tree = auth.tree
    if cascade:
        posts = tree.posts_under(Node.group(unit.id))
    else:
        posts = {post_id for post_id, group_id in tree.post_group.items() if group_id == unit.id}
    return frozenset(posts) - tree.archived_posts


async def archive_unit(request: Request, session: AsyncSession, auth: AuthContext, kind: ScopeGroupKind,
                       unit_id: int, cascade: bool) -> ScopeGroup:
    """POST /corps/{id}/archive, /brigades/{id}/archive: 404 as get_unit; then the permissions, once (403
    permission_required_for_action): scope_group.archive covering its parent (parent_node: the root for a corps),
    with cascade post.view and post.archive covering the unit, without cascade post.view and post.update covering the
    unit and where its posts go (its parent). The post checks are skipped while the request's tree shows no active
    post the archive would change (_posts_changed: for a detach only the posts directly in the unit) and asked after
    all if the database held one and the service archived or moved it; then services/scope_groups.py archive_unit
    (409 corps_has_brigades). One audit row per changed row; committed."""
    unit = await lock_unit(session, auth, kind, unit_id)
    action = f"archive_{ScopeGroupKind(kind).value}"
    AccessControlService.check_covers(auth, Permissions.SCOPE_GROUP_ARCHIVE, parent_node(unit), action)
    check_posts = _check_cascade if cascade else _check_detach
    unchecked = not _posts_changed(auth, unit, cascade)     # the post checks wait for what the database changes
    if not unchecked:
        check_posts(auth, unit, action)
    try:
        changes = await groups_service.archive_unit(session, unit, cascade)
    except CodedError as exc:
        raise ApiError.from_coded(exc)
    if unchecked and (changes.archived_post_ids or changes.moved_post_ids):
        # the request's tree showed no such post, the database held one (committed after the tree was read): the
        # post checks run now, before any audit row; the 403 rolls the request session back
        check_posts(auth, unit, action)

    for change in changes.rows:
        # the snapshots come from the values the service recorded: no query per row
        if change.resource_type == "post":
            params = {"post_name": change.new_values["name"], "post_code": change.new_values["code"]}
            node = Node.post(change.resource_id)
        else:
            params = {"group_name": change.new_values["name"], "group_kind": change.new_values["kind"]}
            node = Node.group(change.resource_id)
        if change.resource_id != unit.id or change.resource_type != "scope_group":
            params["cascade_from"] = unit.id             # a row changed because the unit was archived
        await create_audit_log(
            session, AuditEvent(f"{change.resource_type}.{change.action}"), scope=node,
            resource_id=change.resource_id, params=params,
            user_id=auth.user.id,
            old_values=change.old_values,
            new_values=change.new_values,
            **client_info(request, auth)
        )
    await session.commit()
    await session.refresh(unit)
    return unit


async def restore_unit(request: Request, session: AsyncSession, auth: AuthContext, kind: ScopeGroupKind,
                       unit_id: int) -> ScopeGroup:
    """POST /corps/{id}/restore, /brigades/{id}/restore: the node only. 404 as get_unit (archived ones included);
    scope_group.archive covering its parent (parent_node, 403); not archived: 409 scope_group_not_archived; its parent
    archived: 404 scope_group_not_found (restore the parent first). Audited and committed."""
    unit = await lock_unit(session, auth, kind, unit_id, include_deleted=True)
    AccessControlService.check_covers(auth, Permissions.SCOPE_GROUP_ARCHIVE, parent_node(unit),
                                      f"restore_{ScopeGroupKind(kind).value}")
    if not unit.is_deleted:
        raise ApiError(ErrorCode.SCOPE_GROUP_NOT_ARCHIVED)
    try:
        await groups_service.check_new_parent(session, unit.id, unit.parent_id)
        await groups_service.check_placement(session, kind, unit.parent_id)
    except CodedError as exc:
        raise ApiError.from_coded(exc)
    old_values = unit.model_dump()
    unit.is_deleted = False
    unit.updated_at = datetime.now(timezone.utc)
    session.add(unit)
    await audit_group(request, session, auth, "restore", unit, old_values)
    await session.commit()
    await session.refresh(unit)
    return unit


async def unit_observation_stats(session: AsyncSession, auth: AuthContext, kind: ScopeGroupKind, unit_id: int,
                                 placement: UnitPlacement, date_from: Optional[datetime], date_to: Optional[datetime],
                                 group_by: StatsGroupBy, tz: str, include_archived: bool) -> ObservationStats:
    """GET /corps/{id}/observation-stats, /brigades/{id}/observation-stats. Scope before existence: the unit must lie
    where the caller holds scope_group.view or observation.view, or have a post under it now where observation.view
    is in effect (a brigade's commander counts its corps, a post's viewer its brigade), otherwise 404 like an unknown
    id or one of another kind; an archived unit keeps its history. Counted: the unit's observations (placement) at
    the posts where observation.view is in effect."""
    node = Node.group(unit_id)
    visible = auth.posts_with(Permissions.OBSERVATION_VIEW)
    if not (auth.covers(Permissions.SCOPE_GROUP_VIEW, node) or auth.covers(Permissions.OBSERVATION_VIEW, node)
            or visible is None or visible & unit_post_ids(auth.tree, kind, unit_id)):
        raise ApiError(ErrorCode.SCOPE_GROUP_NOT_FOUND)
    found = (await session.exec(select(ScopeGroup.id).where(
        ScopeGroup.id == unit_id, ScopeGroup.kind == ScopeGroupKind(kind).value))).first()
    if found is None:
        raise ApiError(ErrorCode.SCOPE_GROUP_NOT_FOUND)
    recorded = UnitPlacement(placement) is UnitPlacement.RECORDED
    counted = [Observation.post_id.in_(sorted(visible))] if visible is not None and recorded else []
    if visible is None or recorded:
        condition = and_(observation_unit_condition(auth.tree, kind, unit_id, placement), *counted)
    else:
        # current: one IN list, the posts under the unit now (archived ones left out) that the caller sees
        under = unit_post_ids(auth.tree, kind, unit_id) - auth.tree.archived_posts
        condition = Observation.post_id.in_(sorted(under & visible))
    period_condition = None
    if recorded and auth.tree.group_kind.get(unit_id) == kind.value:
        def period_condition(start: datetime, end: datetime):
            return and_(recorded_in([unit_id], start, end), *counted)
    try:
        return await observation_stats(session, condition, date_from, date_to, group_by, tz, include_archived,
                                       period_condition=period_condition)
    except CodedError as exc:
        raise ApiError.from_coded(exc)
