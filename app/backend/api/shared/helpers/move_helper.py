"""What the move routes share with the upload routes: the answers around a waiting move (services/moves.py)."""
from typing import Any, Dict, Iterable, List, Optional, Set

from fastapi import Request
from sqlalchemy.exc import OperationalError, TimeoutError as PoolTimeout
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.api.shared.helpers.audit_helper import client_info
from app.backend.auth.context import AuthContext
from app.backend.auth.scope import Node
from app.backend.config import settings
from app.backend.enums.errors import ErrorCode
from app.backend.enums.general import MoveStatus
from app.backend.errors.coded import CodedError
from app.backend.errors.problems import ApiError
from app.backend.models.pending_move import PendingMove
from app.backend.services import move_queue, moves
from app.backend.utils.logging_config import get_api_logger
from app.backend.utils.row_locks import start_write

_logger = get_api_logger()

# A look at a move is extra work of a request that has its own answer: a database that refuses it now (a deadlock, a
# lock wait, no connection left in the pool, which is no OperationalError) fails no request. The next look does the same
LOOK_REFUSED = (OperationalError, PoolTimeout)


def retry_after() -> Dict[str, str]:
    """The header of the 202 of a move and of 409 move_pending: the worker's next look at the waiting moves"""
    return {"Retry-After": str(settings.moves.check_interval_seconds)}


def move_pending(move_id: int) -> ApiError:
    return ApiError(ErrorCode.MOVE_PENDING, headers=retry_after(), move_id=move_id)


def changed_fields(row: Any, update_data: Dict[str, Any], move_field: str) -> List[str]:
    """The fields of the request, besides the move, that would change the row: a field sent with its current value
    (a client that sends the whole record) changes nothing"""
    return [name for name, value in update_data.items() if name != move_field and getattr(row, name) != value]


async def admit_move(request: Request, session: AsyncSession, auth: AuthContext, node: Node,
                     target_group_id: Optional[int], locks: moves.MoveLocks, force: bool,
                     changed: List[str]) -> Optional[PendingMove]:
    """moves.admit for the route: None when the move runs now in the route's transaction, the row when it waits. A
    waiting move in the way gets its look first: it runs when it is due, and the request tries again."""
    try:
        return await moves.admit(session, node, target_group_id, auth.user.id, locks, force, changed,
                                 client_info(request, auth))
    except CodedError as exc:
        if exc.code is not ErrorCode.MOVE_PENDING:
            raise ApiError.from_coded(exc)
        await run_due_move(session, exc.params["move_id"])
        raise move_pending(exc.params["move_id"])


def _own_session(session: AsyncSession) -> AsyncSession:
    """A session for the run of a move: the caller's objects stay as they are (a refused run rolls back, which expires
    everything its session holds)"""
    return AsyncSession(session.bind, expire_on_commit=False)


async def run_due_move(session: AsyncSession, move_id: int) -> bool:
    """A request met a waiting move (a refused upload, GET /moves/{id}): when nothing holds it any more it runs now, in
    a transaction of its own, by the claim every runner takes (services/moves.py run_move), so the timeouts need no
    worker. True when the move has ended, and then the request's transaction is over: its locks are gone, its next
    read sees the result. False: it still waits, and the request has only read (a database that refuses the look
    changes nothing either). Call it with no change pending on the session."""
    try:
        if not await moves.is_due(session, move_id):
            return False
        await start_write(session)      # the request's locks and its connection go first
        async with _own_session(session) as own:
            await moves.run_move(own, move_id)
            move = await moves.get_move(own, move_id)
            return move is None or move.status != MoveStatus.WAITING.value
    except LOOK_REFUSED as exc:
        _logger.warning(f"move {move_id} left to the next look: {type(exc).__name__}")
        return False


async def run_released_moves(session: AsyncSession, post_ids: Iterable[int]) -> None:
    """After the commit that ended uploads of these posts: the moves that waited for them run, in a transaction of
    their own. One read on the request's session when no move waits, and no second connection: the pool is the scarce
    thing here. The uploads have ended whatever happens: a database that refuses this work now fails no request, the
    worker's next look does the same."""
    ids: Set[int] = set(post_ids)
    if not ids:
        return
    try:
        # Ids: a refused run rolls back and expires every row its session holds
        move_ids = await move_queue.waiting_above(session, ids)
        if not move_ids:
            return
        await start_write(session)      # one connection at a time
        async with _own_session(session) as own:
            for move_id in move_ids:
                await moves.advance(own, move_id)
    except LOOK_REFUSED as exc:
        _logger.warning(f"moves after an upload of posts {sorted(ids)} left to the next look: {type(exc).__name__}")
