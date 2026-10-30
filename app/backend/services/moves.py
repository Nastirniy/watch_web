"""Moves that wait for uploads (spec 2026-10-30-queued-moves-design).

A move of a post, a group or a brigade with uploads running below it does not run: the request leaves a row in
pending_moves (admit, 202) and new uploads below the node answer 409 move_pending until the move ended. The move runs
when nothing holds it any more (holds: uploads.holds_move; a session gone silent holds nothing and is left alone: it
may go on after the move), once: run_move is called by the end of the last upload (api/shared/helpers/move_helper.py
run_released_moves, in a transaction of its own after that upload's commit), by a request that meets the move when it
is due (run_due_move: a refused upload, a refused move, GET /moves/{id}) and by the media worker, which is a backup
and nothing else. Permissions are read again when it runs, for the user who asked: refused means status failed, the
problem code in error, nothing moved. force runs the move in its request; the uploads go on.

The locks, in the order every writer takes them (lock_for_move):

1. the one row of move_locks: moves run one after another, so two moves can never deadlock on the group rows of two
   nodes going below each other, and "no waiting move overlaps" holds up to the insert of the row;
2. the groups, top down: the old and the new parent and the moved group, then every group below it (locking reads, so
   no snapshot yet);
3. every post below the node, by id (select(Post.id): the Post entity's presence subqueries would take the snapshot);
4. the pending upload sessions of those posts (uploads.lock_pending, through the post_id index, never the one on
   status: an abort holding the row rewrites that one);
5. a run: the pending_moves row, by the conditional UPDATE that claims it. An archive locks that row after its own
   rows and sessions too (move_queue.lock_waiting), and a cancel holds nothing else.

New upload against move: a new session (POST /media/uploads) and the insert of a one-shot upload begin with a shared
lock on the post and read the waiting moves under it (move_queue.waiting_at_post). The move's exclusive lock on the
post conflicts with it. If the upload holds it first, the move waits for the upload's commit and then sees the session
at step 4 (a locking read sees what is committed); if the move holds it first, the upload waits for the move's commit,
and its read, the first plain read of its transaction, sees the waiting row. No third order exists. Chunks and
completes never touch the post row; a move waits for them on the session row at step 4 at most.

Claim: the run's first change is UPDATE ... SET status = done WHERE id = ? AND status = waiting, like the poster claim
of services/media/jobs.py but inside the one transaction of the run: a crash rolls the claim back with the move and
the worker runs it again; a second runner (a worker, a request) waits for the row and matches nothing.

MariaDB snapshot isolation: every row another request may change is locked before the first plain read of the write;
the plain reads of a run (the mover's grants, the tree, the names) come after the claim.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.auth.context import AuthContext, load_auth_context, load_scope_tree
from app.backend.auth.move_access import move_requirements, ungrantable_by_move
from app.backend.auth.scope import Node, ROOT, ScopeTree
from app.backend.enums.audit import AuditEvent
from app.backend.enums.errors import AccessErrorCode, ErrorCode, ProblemType
from app.backend.enums.general import MoveCancelCause, MoveNodeKind, MoveStatus, ScopeGroupKind
from app.backend.errors.coded import CodedError
from app.backend.models.pending_move import PendingMove
from app.backend.models.post import Post
from app.backend.models.scope_group import ScopeGroup
from app.backend.models.upload_session import UploadSession
from app.backend.models.user import User
from app.backend.services import scope_groups as groups_service
from app.backend.services.audit import create_audit_log
from app.backend.services.media import uploads
from app.backend.services.move_queue import kind_of, lock_moves, node_of, waiting_moves
from app.backend.utils.datetime_utils import assume_utc
from app.backend.utils.db_errors import is_unique_violation
from app.backend.utils.logging_config import get_logger
from app.backend.utils.row_locks import lock, lock_all, start_write

_logger = get_logger("moves")


class MoveStep(Enum):
    """What a request that may name a parent asks for, decided on the locked row (step_of)"""
    EDIT = "edit"                       # no move: it sends no parent, or the place it found and left alone
    ALREADY_THERE = "already_there"     # a move that has happened: nothing to write
    CONFLICT = "conflict"               # it does not say where the node is, or thinks it is elsewhere
    MOVE = "move"


@dataclass(frozen=True)
class MoveLocks:
    """What lock_for_move locked"""
    group_ids: Tuple[int, ...]
    post_ids: Tuple[int, ...]
    pending: Tuple[UploadSession, ...]


@dataclass(frozen=True)
class MoveUpload:
    """An upload a move waits for"""
    upload_id: str
    post_id: int
    received_bytes: int
    total_bytes: int
    last_chunk_at: Optional[datetime]
    holding: bool


@dataclass(frozen=True)
class MoveView:
    """A move as GET /moves/{id} answers it"""
    id: int
    status: str
    node_kind: str
    node_id: int
    target_group_id: Optional[int]
    requested_by: int
    requested_at: datetime
    finished_at: Optional[datetime]
    force: bool
    error: Optional[str]
    waiting_seconds: int
    uploads: Tuple[MoveUpload, ...]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _location(group_id: Optional[int]) -> Node:
    return Node.group(group_id) if group_id is not None else ROOT


def step_of(current: Optional[int], target: Optional[int], expected_sent: bool, expected: Optional[int]) -> MoveStep:
    """A request sent the parent target; current is the parent on the locked row, expected what the request says it
    is. Force changes nothing here. The node at the target: a move that has happened when the request expected another
    place; with no expected place, or the place it found, nothing asks for a move, it is an ordinary edit."""
    if target == current:
        return MoveStep.ALREADY_THERE if expected_sent and expected != current else MoveStep.EDIT
    return MoveStep.MOVE if expected_sent and expected == current else MoveStep.CONFLICT


def takes_move_locks(tree: ScopeTree, node: Node, target: Optional[int]) -> bool:
    """A request sent the parent target: does it take the locks of a move? tree is the request's, read before any
    lock, so this only spares the global lock: a request with the current value, one for a target inside the node (a
    cycle, refused by the route's rules in their order). Nothing is decided here: step_of decides on the row, under
    its lock, and a request that turns out to be a move without these locks answers as a stale form (the tree said
    the node sat there and the row says it no longer does: it moved after the request began)."""
    current = tree.post_group.get(node.id) if node.kind == "post" else tree.group_parent.get(node.id)
    if target == current:
        return False
    return target is None or not tree.contains(node, Node.group(target))


def _held(locks: MoveLocks) -> bool:
    """A session lock_for_move locked keeps the move waiting (uploads.holds_move): the read that decides"""
    now = _now()
    return any(uploads.holds_move(row, now) for row in locks.pending)


def locations(tree: ScopeTree, move: PendingMove) -> Tuple[Node, Node]:
    """Where the node sits in the tree, where it goes (the root for a group id None)"""
    parent = tree.post_group.get(move.node_id) if move.node_kind == MoveNodeKind.POST.value \
        else tree.group_parent.get(move.node_id)
    return _location(parent), _location(move.target_group_id)


async def _lock_groups_below(session: AsyncSession, group_id: int) -> List[int]:
    """The group and every group below it, children locked level by level (locking reads: no snapshot), by id within
    a level. The moved group itself is locked by the caller."""
    found, level = [group_id], [group_id]
    while level:
        children = await lock_all(session, select(ScopeGroup.id).where(ScopeGroup.parent_id.in_(level))
                                  .order_by(ScopeGroup.id))
        level = [child for child in children if child not in found]
        found += level
    return found


async def lock_for_move(session: AsyncSession, tree: ScopeTree, node: Node, new_parent: Optional[int]) -> MoveLocks:
    """The locks of a move, the first statements of its write (the module docstring has the order and why). tree
    gives the order of the groups: the request's tree in a route. A plain read comes only after this."""
    await lock_moves(session)
    if node.kind == "post":
        await groups_service.lock_groups(session, tree, [tree.post_group.get(node.id), new_parent], started=True)
        group_ids: List[int] = []
        post_ids = await lock_all(session, select(Post.id).where(Post.id == node.id))
    else:
        await groups_service.lock_groups(session, tree, [tree.group_parent.get(node.id), new_parent, node.id],
                                         started=True)
        group_ids = await _lock_groups_below(session, node.id)
        post_ids = await lock_all(session, select(Post.id).where(Post.group_id.in_(group_ids)).order_by(Post.id))
    pending = await uploads.lock_pending(session, post_ids=post_ids)
    return MoveLocks(tuple(group_ids), tuple(post_ids), tuple(pending))


async def get_move(session: AsyncSession, move_id: int) -> Optional[PendingMove]:
    return (await session.exec(select(PendingMove).where(PendingMove.id == move_id)
                               .execution_options(populate_existing=True))).first()


async def _pending_sessions(session: AsyncSession, post_ids: Iterable[int]) -> List[UploadSession]:
    ids = sorted(post_ids)
    if not ids:
        return []
    query = select(UploadSession).where(UploadSession.post_id.in_(ids), UploadSession.status.in_(uploads.PENDING_STATUSES))
    return list((await session.exec(query.order_by(UploadSession.id).execution_options(populate_existing=True))).all())


async def view_of(session: AsyncSession, move: PendingMove, tree: ScopeTree, with_uploads: bool = True) -> MoveView:
    """The move and, while it waits, the uploads below its node (with_uploads False: none, for a requester who no
    longer holds the permissions at the node)"""
    now = _now()
    rows: List[UploadSession] = []
    if with_uploads and move.status == MoveStatus.WAITING.value:
        rows = await _pending_sessions(session, tree.posts_under(node_of(move)))
    end = assume_utc(move.finished_at) if move.finished_at is not None else now
    return MoveView(
        id=move.id, status=move.status, node_kind=move.node_kind, node_id=move.node_id,
        target_group_id=move.target_group_id, requested_by=move.requested_by, requested_at=move.requested_at,
        finished_at=move.finished_at, force=move.force, error=move.error,
        waiting_seconds=max(0, int((end - assume_utc(move.requested_at)).total_seconds())),
        uploads=tuple(MoveUpload(upload_id=row.id, post_id=row.post_id, received_bytes=int(row.received_bytes),
                                 total_bytes=int(row.expected_size), last_chunk_at=row.last_chunk_at,
                                 holding=uploads.holds_move(row, now)) for row in rows),
    )


async def _audit_params(session: AsyncSession, move: PendingMove) -> dict:
    """The params every move.* event shares: the names as they are now"""
    table = Post if move.node_kind == MoveNodeKind.POST.value else ScopeGroup
    node_name = (await session.exec(select(table.name).where(table.id == move.node_id))).first()
    target_name = None
    if move.target_group_id is not None:
        target_name = (await session.exec(select(ScopeGroup.name).where(ScopeGroup.id == move.target_group_id))).first()
    return {"move_id": move.id, "node_kind": move.node_kind, "node_id": move.node_id, "node_name": node_name,
            "target_group_id": move.target_group_id, "target_name": target_name}


async def _audit(session: AsyncSession, event: AuditEvent, move: PendingMove, user_id: int,
                 client: Optional[Dict[str, Optional[str]]] = None, **params) -> None:
    await create_audit_log(session, event, params={**await _audit_params(session, move), **params},
                           scope=node_of(move), resource_id=move.id, user_id=user_id, **(client or {}))


def _in_the_way(tree: ScopeTree, waiting: PendingMove, moved: Node, target_group_id: Optional[int]) -> bool:
    """A node on the way is busy (owner, fix round): a move of the waiting node, of anything in its subtree or above
    it, a move of anything into its subtree, and a move of its target or of a group that holds the target. So nothing
    joins a node that waits (its uploads would hold a move they never asked for), no two moves wait to go below each
    other, and a target stays where the requester saw it."""
    node = node_of(waiting)
    if tree.contains(node, moved) or tree.contains(moved, node):
        return True
    if target_group_id is not None and tree.contains(node, Node.group(target_group_id)):
        return True
    return waiting.target_group_id is not None and tree.contains(moved, Node.group(waiting.target_group_id))


async def admit(session: AsyncSession, node: Node, target_group_id: Optional[int], user_id: int, locks: MoveLocks,
                force: bool, changed_fields: Sequence[str],
                client: Optional[Dict[str, Optional[str]]] = None) -> Optional[PendingMove]:
    """The decision on a move request, under lock_for_move and after every check of the route, as its last step before
    any change. None: the move runs now in the route's transaction (nothing below the node holds it, or force; a
    forced move over running uploads leaves a done row and a move.completed row in that transaction). A row: the move
    waits; the row and its audit row are committed, or it is the waiting move this request repeats (same node, same
    target), which stays as it is; a forced request is no repeat.

    CodedError move_pending: a waiting move is in the way, force or not (_in_the_way; cancel that one first).
    CodedError move_not_alone: it would have to wait and the request changes other fields too."""
    tree = await load_scope_tree(session)
    for other in await waiting_moves(session):
        if not force and node_of(other) == node and other.target_group_id == target_group_id:
            if changed_fields:
                raise CodedError(ErrorCode.MOVE_NOT_ALONE, changed_fields=sorted(changed_fields))
            await session.commit()
            return other
        if _in_the_way(tree, other, node, target_group_id):
            raise CodedError(ErrorCode.MOVE_PENDING, move_id=other.id)
    if not _held(locks):
        return None
    now = _now()
    if force:
        move = PendingMove(node_kind=kind_of(node).value, node_id=node.id, target_group_id=target_group_id,
                           requested_by=user_id, requested_at=now, status=MoveStatus.DONE.value, force=True,
                           finished_at=now)
        session.add(move)
        await session.flush()
        await _audit(session, AuditEvent.MOVE_COMPLETED, move, user_id, client, forced=True)
        return None
    if changed_fields:
        raise CodedError(ErrorCode.MOVE_NOT_ALONE, changed_fields=sorted(changed_fields))
    move = PendingMove(node_kind=kind_of(node).value, node_id=node.id, target_group_id=target_group_id,
                       requested_by=user_id, requested_at=now)
    session.add(move)
    await session.flush()
    await _audit(session, AuditEvent.MOVE_REQUESTED, move, user_id, client)
    await session.commit()
    _logger.info(f"move {move.id} of {node.key} to {target_group_id} waits for {len(locks.pending)} upload(s)")
    return move


async def _end_waiting(session: AsyncSession, move_id: int, status: MoveStatus, error: Optional[str] = None) -> bool:
    """waiting -> status, one conditional UPDATE; True when this call ended it. The first statement of its write, or
    after the locks of a run: a plain read before it would meet a row changed since (MariaDB error 1020)."""
    result = await session.execute(
        update(PendingMove)
        .execution_options(synchronize_session=False)
        .where(PendingMove.id == move_id, PendingMove.status == MoveStatus.WAITING.value)
        .values(status=status.value, finished_at=_now(), error=error)
    )
    return result.rowcount == 1


async def cancel(session: AsyncSession, move_id: int, user_id: int,
                 client: Optional[Dict[str, Optional[str]]] = None) -> PendingMove:
    """POST /moves/{id}/cancel: the block lifts with this commit. CodedError move_not_waiting when it has ended (a run
    that claimed it first holds its row until it committed: this waits and answers from the result)."""
    await start_write(session)
    cancelled = await _end_waiting(session, move_id, MoveStatus.CANCELLED)
    move = await get_move(session, move_id)
    if not cancelled:
        await session.commit()
        raise CodedError(ErrorCode.MOVE_NOT_WAITING, move_status=move.status)
    await _audit(session, AuditEvent.MOVE_CANCELLED, move, user_id, client, cause=MoveCancelCause.REQUESTED.value)
    await session.commit()
    _logger.info(f"move {move_id} cancelled by user {user_id}")
    return move


async def cancel_locked(session: AsyncSession, moves: Iterable[PendingMove], cause: MoveCancelCause, user_id: int,
                        client: Optional[Dict[str, Optional[str]]] = None) -> None:
    """The waiting moves an archive locked (move_queue.lock_waiting) end cancelled in its transaction (no commit)"""
    now = _now()
    for move in moves:
        move.status = MoveStatus.CANCELLED.value
        move.finished_at = now
        session.add(move)
        await _audit(session, AuditEvent.MOVE_CANCELLED, move, user_id, client, cause=cause.value)
        _logger.info(f"move {move.id} cancelled: {cause.value}")


# ---- running ---------------------------------------------------------------------

async def _mover(session: AsyncSession, move: PendingMove) -> AuthContext:
    """The user who asked, with the grants and the tree of now; a user who can no longer act holds nothing"""
    user = (await session.exec(select(User).where(User.id == move.requested_by))).first()
    if user is None or user.is_deleted or not user.is_active:
        raise CodedError(AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION)
    return await load_auth_context(session, user)


async def _check_mover(session: AsyncSession, auth: AuthContext, kind: MoveNodeKind, old: Node, new: Node,
                       moved: Node) -> None:
    """What the routes ask, as codes: the permissions at both ends, then the H1 rule"""
    if any(not auth.covers(permission, node) for permission, node in move_requirements(kind, old, new)):
        raise CodedError(AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION)
    if await ungrantable_by_move(session, auth, moved, new):
        raise CodedError(AccessErrorCode.GRANT_EXCEEDS_OWN_PERMISSIONS)


async def _apply_post(session: AsyncSession, move: PendingMove, auth: AuthContext) -> None:
    post = await lock(session, select(Post).where(Post.id == move.node_id))
    if post is None or post.is_deleted:
        raise CodedError(ErrorCode.POST_NOT_FOUND)
    if post.group_id == move.target_group_id:
        return
    if move.target_group_id is not None:
        await groups_service.get_group(session, move.target_group_id)
    await _check_mover(session, auth, MoveNodeKind.POST, _location(post.group_id), _location(move.target_group_id),
                       Node.post(post.id))
    old_values = post.model_dump()
    post.group_id = move.target_group_id
    post.updated_at = _now()
    session.add(post)
    await session.flush()
    await create_audit_log(
        session, AuditEvent.POST_UPDATE, scope=Node.post(post.id), resource_id=post.id,
        params={"post_name": post.name, "post_code": post.code}, user_id=move.requested_by,
        old_values=old_values, new_values=post.model_dump(),
    )


async def _apply_group(session: AsyncSession, move: PendingMove, auth: AuthContext) -> None:
    group = await lock(session, select(ScopeGroup).where(ScopeGroup.id == move.node_id))
    if group is None or group.is_deleted:
        raise CodedError(ErrorCode.SCOPE_GROUP_NOT_FOUND)
    target = move.target_group_id
    if group.parent_id == target:
        return
    await groups_service.check_placement(session, ScopeGroupKind(group.kind), target)
    await groups_service.check_new_parent(session, group.id, target)
    await _check_mover(session, auth, MoveNodeKind.GROUP, _location(group.parent_id), _location(target),
                       Node.group(group.id))
    if await groups_service.name_taken(session, group.name, target, exclude_id=group.id):
        raise CodedError(ErrorCode.SCOPE_GROUP_NAME_TAKEN)
    old_values = group.model_dump()
    group.parent_id = target
    group.parent_key = target or 0
    group.updated_at = _now()
    session.add(group)
    await session.flush()
    await create_audit_log(
        session, AuditEvent.SCOPE_GROUP_UPDATE, scope=Node.group(group.id), resource_id=group.id,
        params={"group_name": group.name, "group_kind": group.kind}, user_id=move.requested_by,
        old_values=old_values, new_values=group.model_dump(),
    )


async def _fail(session: AsyncSession, move_id: int, code: ProblemType) -> None:
    """Nothing moved: the run's transaction is gone, the move ends failed in a new one"""
    await session.rollback()
    if await _end_waiting(session, move_id, MoveStatus.FAILED, code.value):
        move = await get_move(session, move_id)
        await _audit(session, AuditEvent.MOVE_FAILED, move, move.requested_by, failure_code=code.value)
        _logger.warning(f"move {move_id} failed: {code.value}")
    await session.commit()


async def run_move(session: AsyncSession, move_id: int) -> Optional[MoveStatus]:
    """Run a waiting move, in one transaction: the locks, the claim, the checks of now, the change, the audit rows,
    the commit. The status it ended in; None when this call changed nothing: it no longer waits, another runner has
    it, or an upload below the node still holds it. Sessions that no longer hold it are locked and left as they are."""
    move = await get_move(session, move_id)
    if move is None or move.status != MoveStatus.WAITING.value:
        return None
    tree = await load_scope_tree(session)
    locks = await lock_for_move(session, tree, node_of(move), move.target_group_id)
    if _held(locks) or not await _end_waiting(session, move_id, MoveStatus.DONE):
        await session.commit()
        return None
    try:
        if move.node_kind == MoveNodeKind.POST.value:
            await _apply_post(session, move, await _mover(session, move))
        else:
            await _apply_group(session, move, await _mover(session, move))
        await _audit(session, AuditEvent.MOVE_COMPLETED, move, move.requested_by)
        await session.commit()
    except CodedError as exc:
        await _fail(session, move_id, exc.code)
        return MoveStatus.FAILED
    except IntegrityError as exc:
        # The name index: a group created at the target after the check (a create takes no lock on its parent)
        if not is_unique_violation(exc):
            raise
        await _fail(session, move_id, ErrorCode.SCOPE_GROUP_NAME_TAKEN)
        return MoveStatus.FAILED
    _logger.info(f"move {move_id} of {node_of(move).key} to {move.target_group_id} done")
    return MoveStatus.DONE


async def is_due(session: AsyncSession, move_id: int) -> bool:
    """A waiting move nothing below its node holds any more: the plain read of a look at it, on any session. The
    decision is run_move's, under its locks: this only spares a look the global lock while uploads run."""
    move = await get_move(session, move_id)
    if move is None or move.status != MoveStatus.WAITING.value:
        return False
    now = _now()
    rows = await _pending_sessions(session, (await load_scope_tree(session)).posts_under(node_of(move)))
    return not any(uploads.holds_move(row, now) for row in rows)


async def advance(session: AsyncSession, move_id: int) -> Optional[MoveStatus]:
    """One look at a waiting move, with a session of its own (it commits): when nothing holds it any more it runs. A
    look touches no upload, whatever the run decides. run_move's answer, None when it did not run."""
    return await run_move(session, move_id) if await is_due(session, move_id) else None
