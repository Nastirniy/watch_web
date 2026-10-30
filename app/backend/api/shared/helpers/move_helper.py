"""What the move routes share with the upload routes: the answers around a waiting move (services/moves.py)."""
from typing import Any, Dict, Iterable, List, Optional, Set

from fastapi import Request
from sqlalchemy.exc import OperationalError
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.api.shared.helpers.audit_helper import client_info
from app.backend.api.shared.helpers.media_helper import storage_root
from app.backend.auth.context import AuthContext
from app.backend.auth.scope import Node
from app.backend.config import settings
from app.backend.enums.errors import ErrorCode
from app.backend.errors.coded import CodedError
from app.backend.errors.problems import ApiError
from app.backend.models.pending_move import PendingMove
from app.backend.services import moves
from app.backend.utils.logging_config import get_api_logger

_logger = get_api_logger()


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
    """moves.admit for the route: None when the move runs now in the route's transaction, the row when it waits"""
    try:
        return await moves.admit(session, node, target_group_id, auth.user.id, locks, force, changed,
                                 client_info(request, auth))
    except CodedError as exc:
        raise ApiError.from_coded(exc, headers=retry_after() if exc.code is ErrorCode.MOVE_PENDING else None)


async def run_released_moves(session: AsyncSession, post_ids: Iterable[int]) -> None:
    """After the commit that ended uploads of these posts: the moves that waited for them run, in a transaction of
    their own. The uploads have ended whatever happens here: a database that refuses this work now fails no request,
    the worker's next look at the waiting moves does the same."""
    ids: Set[int] = set(post_ids)
    if not ids:
        return
    try:
        await moves.after_uploads_ended(session.bind, ids, storage_root())
    except OperationalError as exc:
        _logger.warning(f"moves after an upload of posts {sorted(ids)} left to the worker: {type(exc).__name__}")
