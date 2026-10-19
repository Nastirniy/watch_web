"""Scope groups: rules of the tree (existence, cycles, depth, emptiness). Raises CodedError; the route answers."""
from typing import Dict, Optional

from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.utils.row_locks import lock_first

from app.backend.enums.errors import ErrorCode
from app.backend.errors.coded import CodedError
from app.backend.models.post import Post
from app.backend.models.scope_group import ScopeGroup

# Levels of nested groups between the root and a post: a root group is level 1
MAX_GROUP_DEPTH = 6


async def get_group(session: AsyncSession, group_id: int, include_deleted: bool = False, lock: bool = False) -> ScopeGroup:
    """The group; lock=True locks it as the first statement of the write (utils/row_locks.py)"""
    query = select(ScopeGroup).where(ScopeGroup.id == group_id)
    if not include_deleted:
        query = query.where(ScopeGroup.is_deleted == False)  # noqa: E712
    group = await lock_first(session, query) if lock else (await session.exec(query)).first()
    if group is None:
        raise CodedError(ErrorCode.SCOPE_GROUP_NOT_FOUND)
    return group


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
