"""Reading the audit log: the rows a caller may see (AuditVisibility), filtered by AuditCriteria, newest first.

A page costs three queries whatever its size: the rows, the actors' usernames (as they are now), the names of the
scope nodes (as they are now). The service returns AuditEntry dataclasses; each API version builds its response
(one model per event) from them. No FastAPI.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Tuple

from sqlalchemy import false, literal, or_, true
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.auth.context import AuthContext
from app.backend.auth.scope import Node, NodeKind, ScopeTree
from app.backend.config import settings
from app.backend.enums.audit import AuditCategory, AuditEvent
from app.backend.enums.general import Permissions, ScopeGroupKind, SortOrder
from app.backend.filters.simple_engine import ListCriteria, SimpleFilterEngine, stored_value
from app.backend.models.audit import AuditLog
from app.backend.models.post import Post
from app.backend.models.scope_group import ScopeGroup
from app.backend.models.user import User
from app.backend.services.units import is_unit

ROOT_KEY = Node.root().key
CATALOG_CODES = tuple(event.code for event in AuditEvent)
_DESCENDING = (SortOrder.DESCENDING, SortOrder.DESCENDING_NULLS_FIRST, SortOrder.DESCENDING_NULLS_LAST)


# ------------------------------------------------------------------ who sees what

@dataclass(frozen=True)
class AuditVisibility:
    """The rows a caller may read. everything: every row (audit.view). Otherwise rows whose scope post is in posts or
    whose scope group is in groups (None: every post / every group, and the root: a grant at the root); a row without
    a scope never. device_fields: ip_address, user_agent, session_id and request_url are shown (audit.view only)."""
    everything: bool
    posts: Optional[FrozenSet[int]]
    groups: Optional[FrozenSet[int]]
    device_fields: bool


EVERYTHING = AuditVisibility(everything=True, posts=None, groups=None, device_fields=True)
NOTHING = AuditVisibility(everything=False, posts=frozenset(), groups=frozenset(), device_fields=False)


def visibility_of(auth: AuthContext) -> AuditVisibility:
    """What the caller may read of the audit log. audit.view (system, held at the root through a system role): every
    row and every column. Otherwise audit.view_scope (tree): the rows of the posts where it is in effect (posts_with:
    a group grant reaches the archived posts below it, a grant on an archived post applies nowhere), the rows of the
    groups inside a subtree where it is held (groups_with) and, held at the root, the rows at the root; never a row
    without a scope, never the device fields. Without either permission nothing (the route's require() answers 403
    before)."""
    if auth.has_anywhere(Permissions.AUDIT_VIEW):
        return EVERYTHING
    if auth.has_anywhere(Permissions.AUDIT_VIEW_SCOPE):
        return AuditVisibility(everything=False, posts=auth.posts_with(Permissions.AUDIT_VIEW_SCOPE),
                               groups=auth.groups_with(Permissions.AUDIT_VIEW_SCOPE), device_fields=False)
    return NOTHING


def visible_clause(visibility: AuditVisibility) -> ColumnElement:
    if visibility.everything:
        return true()
    parts = []
    if visibility.posts is None:
        parts.append(AuditLog.scope_post_id.is_not(None))
        parts.append(AuditLog.scope_key == ROOT_KEY)
    elif visibility.posts:
        parts.append(AuditLog.scope_post_id.in_(sorted(visibility.posts)))
    if visibility.groups is None:
        parts.append(AuditLog.scope_group_id.is_not(None))
    elif visibility.groups:
        parts.append(AuditLog.scope_group_id.in_(sorted(visibility.groups)))
    return or_(*parts) if parts else false()


# ------------------------------------------------------------------ criteria

@dataclass(frozen=True, kw_only=True)
class AuditCriteria(ListCriteria):
    """What the audit log is filtered by; each API version converts its query model into it (the same names).

    event / category: any of these stored values (legacy.unknown also matches rows without an event or with a code
    the catalog does not have). post_id: rows at that post.
    group_id, corps_id, brigade_id: rows at the node or below it in the scope tree as it is now (corps_id / brigade_id
    of a group of another kind: nothing). Several given: all must hold. session_id needs the device fields (without
    them it matches nothing). before_id: rows with a smaller id (the keyset of the default order, id descending)."""
    sort_order: SortOrder = SortOrder.DESCENDING
    event: Optional[List[str]] = None
    category: Optional[List[AuditCategory]] = None
    user_id: Optional[int] = None
    resource_type: Optional[str] = None
    resource_id: Optional[int] = None
    success: Optional[bool] = None
    created_from: Optional[datetime] = None
    created_to: Optional[datetime] = None
    request_id: Optional[str] = None
    session_id: Optional[str] = None
    post_id: Optional[int] = None
    group_id: Optional[int] = None
    corps_id: Optional[int] = None
    brigade_id: Optional[int] = None
    before_id: Optional[int] = None


def _at_or_below(tree: ScopeTree, group_id: int) -> ColumnElement:
    """Rows at the group (known to the tree or not) or anywhere below it"""
    node = Node.group(group_id)
    groups = tree.groups_under(node) | {group_id}
    posts = tree.posts_under(node)
    return or_(AuditLog.scope_group_id.in_(sorted(groups)), AuditLog.scope_post_id.in_(sorted(posts)))


class AuditFilterService(SimpleFilterEngine):
    """The filters of the audit log list"""

    MODEL = AuditLog
    CRITERIA = AuditCriteria
    SORTABLE_FIELDS = ["id"]

    @classmethod
    def apply_filters(cls, query, params: AuditCriteria, visibility: AuditVisibility, tree: ScopeTree):
        params = cls.criteria(params)
        query = query.where(visible_clause(visibility))
        if params.event:
            codes = [stored_value(code) for code in params.event]
            matches = AuditLog.event.in_(codes)
            if AuditEvent.LEGACY_UNKNOWN.code in codes:
                matches = or_(matches, AuditLog.event.is_(None), AuditLog.event.not_in(CATALOG_CODES))
            query = query.where(matches)
        query = cls._apply_in_list(query, AuditLog.category, params.category)
        query = cls._apply_exact(query, AuditLog.user_id, params.user_id)
        query = cls._apply_exact(query, AuditLog.resource_type, params.resource_type)
        query = cls._apply_exact(query, AuditLog.resource_id, params.resource_id)
        query = cls._apply_exact(query, AuditLog.success, params.success)
        query = cls._apply_range(query, AuditLog.created_at, params.created_from, params.created_to)
        query = cls._apply_exact(query, AuditLog.request_id, params.request_id)
        if params.session_id is not None:
            query = query.where(AuditLog.session_id == params.session_id if visibility.device_fields else false())
        query = cls._apply_exact(query, AuditLog.scope_post_id, params.post_id)
        if params.group_id is not None:
            query = query.where(_at_or_below(tree, params.group_id))
        for kind, unit_id in ((ScopeGroupKind.CORPS, params.corps_id), (ScopeGroupKind.BRIGADE, params.brigade_id)):
            if unit_id is not None:
                query = query.where(_at_or_below(tree, unit_id) if is_unit(tree, kind, unit_id) else false())
        if params.before_id is not None:
            query = query.where(AuditLog.id < params.before_id)
        order = AuditLog.id.desc() if params.sort_order in _DESCENDING else AuditLog.id.asc()
        return cls._apply_pagination(query.order_by(order), params)


# ------------------------------------------------------------------ entries

@dataclass(frozen=True)
class AuditActor:
    id: int
    username: Optional[str]     # as it is now; None for an id no user has


@dataclass(frozen=True)
class AuditScope:
    kind: NodeKind
    id: Optional[int]
    name: Optional[str]         # as it is now; the application name at the root; None for a node that is gone


@dataclass(frozen=True)
class AuditEntry:
    """One audit row as a reader sees it. event / category as stored (None on old rows); message: the English
    sentence old rows kept in details (None on new rows). The device fields are None without visibility of them."""
    id: int
    created_at: datetime
    event: Optional[str]
    category: Optional[str]
    action: str
    resource_type: str
    resource_id: Optional[int]
    params: Optional[Dict[str, Any]]
    message: Optional[str]
    actor: Optional[AuditActor]
    scope: Optional[AuditScope]
    success: bool
    old_values: Optional[Dict[str, Any]]
    new_values: Optional[Dict[str, Any]]
    request_id: Optional[str]
    ip_address: Optional[str] = field(default=None)
    user_agent: Optional[str] = field(default=None)
    session_id: Optional[str] = field(default=None)
    request_url: Optional[str] = field(default=None)


def _node(row: AuditLog) -> Optional[Node]:
    if row.scope_post_id is not None:
        return Node.post(row.scope_post_id)
    if row.scope_group_id is not None:
        return Node.group(row.scope_group_id)
    if row.scope_key is not None:
        try:
            return Node.parse(row.scope_key)
        except ValueError:
            return None
    return None


async def _usernames(session: AsyncSession, user_ids: Iterable[int]) -> Dict[int, str]:
    """One query; plain columns (never the User entity: its presence properties read user_sessions)"""
    ids = sorted(set(user_ids))
    if not ids:
        return {}
    return dict((await session.exec(select(User.id, User.username).where(User.id.in_(ids)))).all())


async def _node_names(session: AsyncSession, nodes: Iterable[Node]) -> Dict[Tuple[str, int], str]:
    """(kind, id) -> name of the posts and groups, one query"""
    nodes = list(nodes)
    posts = sorted({node.id for node in nodes if node.kind == "post"})
    groups = sorted({node.id for node in nodes if node.kind == "group"})
    parts = []
    if posts:
        parts.append(select(literal("post").label("kind"), Post.id, Post.name).where(Post.id.in_(posts)))
    if groups:
        parts.append(select(literal("group").label("kind"), ScopeGroup.id, ScopeGroup.name)
                     .where(ScopeGroup.id.in_(groups)))
    if not parts:
        return {}
    query = parts[0] if len(parts) == 1 else parts[0].union_all(parts[1])
    return {(kind, node_id): name for kind, node_id, name in (await session.exec(query)).all()}


def _message(details: Optional[Dict[str, Any]]) -> Optional[str]:
    message = details.get("message") if isinstance(details, dict) else None
    return message if isinstance(message, str) else None


async def _entries(session: AsyncSession, rows: List[AuditLog], visibility: AuditVisibility) -> List[AuditEntry]:
    nodes = {row.id: _node(row) for row in rows}
    usernames = await _usernames(session, (row.user_id for row in rows if row.user_id is not None))
    names = await _node_names(session, (node for node in nodes.values() if node is not None))

    def scope_of(node: Optional[Node]) -> Optional[AuditScope]:
        if node is None:
            return None
        if node.kind == "root":
            return AuditScope(kind="root", id=None, name=settings.app.name)
        return AuditScope(kind=node.kind, id=node.id, name=names.get((node.kind, node.id)))

    device = visibility.device_fields
    return [AuditEntry(
        id=row.id, created_at=row.created_at, event=row.event, category=row.category, action=row.action,
        resource_type=row.resource_type, resource_id=row.resource_id, params=row.params, message=_message(row.details),
        actor=None if row.user_id is None else AuditActor(id=row.user_id, username=usernames.get(row.user_id)),
        scope=scope_of(nodes[row.id]), success=row.success, old_values=row.old_values, new_values=row.new_values,
        request_id=row.request_id,
        ip_address=row.ip_address if device else None, user_agent=row.user_agent if device else None,
        session_id=row.session_id if device else None, request_url=row.request_url if device else None,
    ) for row in rows]


async def list_entries(session: AsyncSession, criteria: AuditCriteria, visibility: AuditVisibility,
                       tree: ScopeTree) -> List[AuditEntry]:
    """A page of the rows the caller may see that match the criteria (tree: the scope tree of the request, for
    group_id / corps_id / brigade_id), newest first by default. Three queries."""
    query = AuditFilterService.apply_filters(select(AuditLog), criteria, visibility, tree)
    rows = list((await session.exec(query)).all())
    return await _entries(session, rows, visibility)


async def entry_exists(session: AsyncSession, audit_id: int) -> bool:
    """A row with the id exists, visible or not (tells a hidden entry from a missing one)"""
    return (await session.exec(select(AuditLog.id).where(AuditLog.id == audit_id))).first() is not None


async def get_entry(session: AsyncSession, audit_id: int, visibility: AuditVisibility) -> Optional[AuditEntry]:
    """One row by id, None when there is none or the caller may not see it (entry_exists tells which)"""
    row = (await session.exec(
        select(AuditLog).where(AuditLog.id == audit_id, visible_clause(visibility)))).first()
    if row is None:
        return None
    (entry,) = await _entries(session, [row], visibility)
    return entry
