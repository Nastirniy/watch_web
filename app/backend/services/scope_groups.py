"""Scope groups: rules of the tree (existence, cycles, depth, emptiness, where each kind may sit). Raises CodedError;
the route answers."""
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Tuple

from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.auth.scope import Node, ScopeTree
from app.backend.utils.row_locks import lock, lock_all, lock_first

from app.backend.enums.errors import ErrorCode
from app.backend.enums.general import ScopeGroupKind
from app.backend.errors.coded import CodedError
from app.backend.models.pending_move import PendingMove
from app.backend.models.post import Post
from app.backend.models.scope_group import ScopeGroup
from app.backend.models.upload_session import UploadSession
from app.backend.services import move_queue
from app.backend.services.media import uploads

# Levels of nested groups between the root and a post: a root group is level 1
MAX_GROUP_DEPTH = 6

# Where each kind of group may sit: the kinds of parent it accepts, None for directly under the root. A new level (a
# battalion) is a ScopeGroupKind member, its entry here and its place in UNIT_KINDS; no query knows the levels.
ALLOWED_PARENT: Dict[ScopeGroupKind, FrozenSet[Optional[ScopeGroupKind]]] = {
    ScopeGroupKind.GROUP: frozenset({None, ScopeGroupKind.GROUP}),
    ScopeGroupKind.CORPS: frozenset({None}),
    ScopeGroupKind.BRIGADE: frozenset({ScopeGroupKind.CORPS}),
}

# The kinds that are units of the organisation, top down (fields of posts and observations, report columns)
UNIT_KINDS: Tuple[ScopeGroupKind, ...] = (ScopeGroupKind.CORPS, ScopeGroupKind.BRIGADE)


async def get_group(session: AsyncSession, group_id: int, include_deleted: bool = False, lock: bool = False) -> ScopeGroup:
    """The group; lock=True locks it as the first statement of the write (utils/row_locks.py)"""
    query = select(ScopeGroup).where(ScopeGroup.id == group_id)
    if not include_deleted:
        query = query.where(ScopeGroup.is_deleted == False)  # noqa: E712
    group = await lock_first(session, query) if lock else (await session.exec(query)).first()
    if group is None:
        raise CodedError(ErrorCode.SCOPE_GROUP_NOT_FOUND)
    return group


async def name_taken(session: AsyncSession, name: str, parent_id: Optional[int], exclude_id: Optional[int] = None) -> bool:
    """Case-insensitive, archived groups included: the name is unique within its parent (parent_key 0 is the root)"""
    query = select(ScopeGroup.id).where(
        ScopeGroup.parent_key == (parent_id or 0), func.lower(ScopeGroup.name) == name.lower(),
    )
    if exclude_id is not None:
        query = query.where(ScopeGroup.id != exclude_id)
    return (await session.exec(query)).first() is not None


def _top_down(tree: ScopeTree, group_id: int) -> tuple:
    """Sort key of the lock order: depth in the tree, then id"""
    return len(tree.ancestors(Node.group(group_id))), group_id


async def lock_groups(session: AsyncSession, tree: ScopeTree, group_ids: Iterable[Optional[int]],
                      started: bool = False) -> bool:
    """Lock these groups top down (the first with lock_first unless the write has started); False when there is none
    and the write had not begun. Only groups the tree knows: a lock on a row that may not exist would take a gap lock
    that holds up concurrent creates (the caller's own existence check answers 404 for an unknown id)."""
    ordered = sorted({group_id for group_id in group_ids if group_id in tree.group_parent},
                     key=lambda g: _top_down(tree, g))
    for position, group_id in enumerate(ordered):
        await (lock if position or started else lock_first)(session, select(ScopeGroup).where(ScopeGroup.id == group_id))
    return started or bool(ordered)


async def _live_parents(session: AsyncSession) -> Dict[int, Optional[int]]:
    rows = await session.exec(select(ScopeGroup.id, ScopeGroup.parent_id).where(ScopeGroup.is_deleted == False))  # noqa: E712
    return {row[0]: row[1] for row in rows.all()}


def _height(group_id: int, parents: Dict[int, Optional[int]]) -> int:
    """Levels of the subtree under group_id, the group itself included"""
    children: Dict[int, list] = {}
    for child, parent in parents.items():
        if parent is not None:
            children.setdefault(parent, []).append(child)
    height, level = 0, [group_id]
    while level:
        height += 1
        level = [c for g in level for c in children.get(g, [])]
    return height


async def check_new_parent(session: AsyncSession, group_id: Optional[int], parent_id: Optional[int]) -> None:
    """The group (None: a new one) may sit under parent_id (None: the root).

    The parent exists and is not archived, is not the group itself or one of its descendants, and the parent's
    depth plus the height of the moved subtree stays within MAX_GROUP_DEPTH.
    """
    if parent_id is None:
        return
    await get_group(session, parent_id)
    parents = await _live_parents(session)

    depth = 0  # levels from the root down to the parent
    cursor: Optional[int] = parent_id
    while cursor is not None:
        if cursor == group_id:
            raise CodedError(ErrorCode.SCOPE_GROUP_CYCLE)
        depth += 1
        cursor = parents.get(cursor)

    height = _height(group_id, parents) if group_id is not None and group_id in parents else 1
    if depth + height > MAX_GROUP_DEPTH:
        raise CodedError(ErrorCode.SCOPE_GROUP_DEPTH_EXCEEDED, max_depth=MAX_GROUP_DEPTH)


async def check_empty(session: AsyncSession, group_id: int) -> None:
    """No live subgroup and no live post in the group"""
    groups = (await session.exec(
        select(func.count()).select_from(ScopeGroup).where(ScopeGroup.parent_id == group_id, ScopeGroup.is_deleted == False)  # noqa: E712
    )).one()
    posts = (await session.exec(
        select(func.count()).select_from(Post).where(Post.group_id == group_id, Post.is_deleted == False)  # noqa: E712
    )).one()
    if groups or posts:
        raise CodedError(ErrorCode.SCOPE_GROUP_NOT_EMPTY)


async def check_placement(session: AsyncSession, kind: ScopeGroupKind, parent_id: Optional[int]) -> None:
    """A group of this kind may sit under parent_id (None: the root): ALLOWED_PARENT, the parent's kind read from its
    row (archived ones included: existence is check_new_parent's). 409 scope_group_parent_not_allowed otherwise."""
    parent_kind: Optional[ScopeGroupKind] = None
    if parent_id is not None:
        stored = (await session.exec(select(ScopeGroup.kind).where(ScopeGroup.id == parent_id))).first()
        if stored is None:
            raise CodedError(ErrorCode.SCOPE_GROUP_NOT_FOUND)
        parent_kind = ScopeGroupKind(stored)
    kind = ScopeGroupKind(kind)
    if parent_kind not in ALLOWED_PARENT[kind]:
        raise CodedError(ErrorCode.SCOPE_GROUP_PARENT_NOT_ALLOWED, group_kind=kind.value,
                         parent_kind=parent_kind.value if parent_kind is not None else "root")


@dataclass(frozen=True)
class RowChange:
    """One row an archive changed, for its audit row"""
    resource_type: str      # "post" or "scope_group"
    resource_id: int
    action: str             # "archive", or "update" for a post moved out of the unit
    old_values: Dict[str, Any]
    new_values: Dict[str, Any]


@dataclass
class ArchiveChanges:
    """What archive_unit changed (the unit itself last in archived_group_ids and in rows)"""
    archived_group_ids: List[int] = field(default_factory=list)
    archived_post_ids: List[int] = field(default_factory=list)
    moved_post_ids: List[int] = field(default_factory=list)
    rows: List[RowChange] = field(default_factory=list)
    # cascade: the pending upload sessions of the archived posts, locked (services/media/uploads.py lock_pending);
    # the caller cancels them in its transaction
    pending_uploads: List[UploadSession] = field(default_factory=list)
    # the waiting moves of the archived groups and posts, locked (services/move_queue.py lock_waiting); the caller
    # cancels them in its transaction
    waiting_moves: List[PendingMove] = field(default_factory=list)


async def archive_unit(session: AsyncSession, unit: ScopeGroup, cascade: bool) -> ArchiveChanges:
    """Archive a unit (corps, brigade) that the caller already locked (lock_first) and checked the permissions for.

    Locks the active units inside it, then the active posts it holds directly or through them, each in id order
    (groups before posts, as every writer does). cascade=True archives all of them; otherwise the posts move one level
    up (a brigade's into its corps, a corps' under the root), and a corps with an active brigade raises
    corps_has_brigades before anything changes. Posts archived before stay as they are. With cascade the posts are
    locked by id first, then their pending upload sessions (pending_uploads) and the waiting moves of what is archived
    (waiting_moves), then the Post entities, whose presence subqueries take the snapshot: a chunk writer renewing its
    claim, a cancel or a run of a move meanwhile wait for the archive. The caller cancels pending_uploads and
    waiting_moves, writes the audit rows (rows) and commits.
    """
    changes = ArchiveChanges()
    now = datetime.now(timezone.utc)
    children = await lock_all(session, select(ScopeGroup).where(
        ScopeGroup.parent_id == unit.id, ScopeGroup.is_deleted == False).order_by(ScopeGroup.id))  # noqa: E712
    if children and not cascade:
        raise CodedError(ErrorCode.CORPS_HAS_BRIGADES)
    holders = [unit.id, *(child.id for child in children)]
    archived_nodes = [Node.group(group_id) for group_id in holders]
    if cascade:
        post_ids = await lock_all(session, select(Post.id).where(
            Post.group_id.in_(holders), Post.is_deleted == False).order_by(Post.id))  # noqa: E712
        changes.pending_uploads = await uploads.lock_pending(session, post_ids=post_ids)
        archived_nodes += [Node.post(post_id) for post_id in post_ids]
    changes.waiting_moves = await move_queue.lock_waiting(session, archived_nodes)
    posts = await lock_all(session, select(Post).where(
        Post.group_id.in_(holders), Post.is_deleted == False).order_by(Post.id))  # noqa: E712

    for post in posts:
        old_values = post.model_dump()
        if cascade:
            post.is_deleted = True
            changes.archived_post_ids.append(post.id)
        else:
            post.group_id = unit.parent_id
            changes.moved_post_ids.append(post.id)
        post.updated_at = now
        session.add(post)
        changes.rows.append(RowChange("post", post.id, "archive" if cascade else "update", old_values, post.model_dump()))
    for group in (*children, unit):
        old_values = group.model_dump()
        group.is_deleted = True
        group.updated_at = now
        session.add(group)
        changes.archived_group_ids.append(group.id)
        changes.rows.append(RowChange("scope_group", group.id, "archive", old_values, group.model_dump()))
    return changes
