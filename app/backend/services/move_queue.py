"""The queue of waiting moves: what the writes around a move read and lock. services/moves.py is the move itself.

Every write that moves a node takes the one row of move_locks first (lock_moves): moves run one after another whatever
nodes they touch, so two moves of nodes that overlap, or of two nodes each going below the other, never deadlock on
their group rows, and the check "no waiting move overlaps" and the insert of a waiting row stand together.

A new upload reads the waiting moves under its shared lock on the post (waiting_at_post), the move locks every post
below its node (services/moves.py lock_for_move): whichever comes second sees the other (the module docstring there).

An archive locks the waiting moves of what it archives (lock_waiting) after its own rows and before its first plain
read: a cancel or a run that changes such a row meanwhile waits for the archive instead of meeting it (MariaDB error
1020). The order of every writer: groups, posts, upload sessions, then the pending_moves rows.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, List, Optional

from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.auth.context import load_scope_tree
from app.backend.auth.scope import Node
from app.backend.enums.general import MoveNodeKind, MoveStatus
from app.backend.models.pending_move import MoveLock, PendingMove
from app.backend.utils.row_locks import lock_all, lock_first

MOVE_LOCK_ID = 1


def node_of(move: PendingMove) -> Node:
    """The node of a move as a node of the scope tree"""
    return Node.post(move.node_id) if move.node_kind == MoveNodeKind.POST.value else Node.group(move.node_id)


def kind_of(node: Node) -> MoveNodeKind:
    return MoveNodeKind.POST if node.kind == "post" else MoveNodeKind.GROUP


async def lock_moves(session: AsyncSession) -> None:
    """The first statement of a write that moves a node: moves run one after another"""
    await lock_first(session, select(MoveLock.id).where(MoveLock.id == MOVE_LOCK_ID))


async def waiting_moves(session: AsyncSession) -> List[PendingMove]:
    query = select(PendingMove).where(PendingMove.status == MoveStatus.WAITING.value).order_by(PendingMove.id)
    return list((await session.exec(query.execution_options(populate_existing=True))).all())


async def waiting_at_post(session: AsyncSession, post_id: int) -> Optional[int]:
    """The id of the waiting move whose subtree holds the post, None when there is none. One indexed read when no
    move waits, the scope tree only when one does; for the authoritative answer of a new upload, read it under the
    shared lock on the post (a move locks the post before it can commit its row, and cannot go on until the upload
    has committed)."""
    moves = await waiting_moves(session)
    if not moves:
        return None
    tree = await load_scope_tree(session)
    post = Node.post(post_id)
    return next((move.id for move in moves if tree.contains(node_of(move), post)), None)


async def waiting_above(session: AsyncSession, post_ids: Iterable[int]) -> List[int]:
    """The ids of the waiting moves whose subtree holds one of the posts. One indexed read when no move waits."""
    moves = await waiting_moves(session)
    if not moves:
        return []
    tree = await load_scope_tree(session)
    posts = [Node.post(post_id) for post_id in post_ids]
    return [move.id for move in moves if any(tree.contains(node_of(move), post) for post in posts)]


@dataclass(frozen=True)
class WaitingReport:
    """The waiting moves, counts only (/health)"""
    count: int
    oldest_requested_at: Optional[datetime]


async def waiting_report(session: AsyncSession) -> WaitingReport:
    count, oldest = (await session.exec(select(func.count(), func.min(PendingMove.requested_at))
                                        .where(PendingMove.status == MoveStatus.WAITING.value))).one()
    return WaitingReport(count, oldest)


async def lock_waiting(session: AsyncSession, nodes: Iterable[Node]) -> List[PendingMove]:
    """The waiting moves of these nodes, locked (id order): a later lock of an archive, after its rows and sessions,
    before its first plain read. The caller ends them in its transaction (services/moves.py cancel_locked)."""
    moved = list(nodes)
    post_ids = sorted(node.id for node in moved if node.kind == "post")
    group_ids = sorted(node.id for node in moved if node.kind == "group")
    found: List[PendingMove] = []
    for kind, ids in ((MoveNodeKind.POST, post_ids), (MoveNodeKind.GROUP, group_ids)):
        if ids:
            found += await lock_all(session, select(PendingMove).where(
                PendingMove.status == MoveStatus.WAITING.value, PendingMove.node_kind == kind.value,
                PendingMove.node_id.in_(ids)).order_by(PendingMove.id))
    return found
