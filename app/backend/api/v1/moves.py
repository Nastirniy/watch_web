"""Queued moves: a move of a post, a scope group or a brigade that waits for the uploads below it.

A move is made on its own resource (PUT /posts/{id}, PATCH /scope-groups/{id}, PATCH /brigades/{id}); these routes follow
and cancel the operation it left. Who sees it: the permissions a move needs, in effect at the node, or the user who
asked (they must learn how it ended, whatever they lost meanwhile). Anyone else gets 404 like an unknown id.
"""
from fastapi import APIRouter, Depends, Request
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.api.shared.helpers.audit_helper import client_info
from app.backend.api.v1.models.response.moves import MoveResponse, move_response
from app.backend.auth.access_control import AccessControlService
from app.backend.auth.context import AuthContext
from app.backend.auth.move_access import move_requirements, view_requirements
from app.backend.auth.require import require
from app.backend.database import get_session
from app.backend.enums.errors import AccessErrorCode, ErrorCode
from app.backend.enums.general import MoveNodeKind
from app.backend.enums.routes import MoveRoutes
from app.backend.errors.coded import CodedError
from app.backend.errors.openapi import problems
from app.backend.errors.problems import ApiError
from app.backend.models.pending_move import PendingMove
from app.backend.services import moves
from app.backend.services.move_queue import node_of

router = APIRouter(prefix=MoveRoutes.BASE.value, tags=[MoveRoutes.tag()])


def _holds_view(auth: AuthContext, move: PendingMove) -> bool:
    return all(auth.covers(permission, node_of(move)) for permission in view_requirements(MoveNodeKind(move.node_kind)))


async def _visible_move(session: AsyncSession, auth: AuthContext, move_id: int) -> PendingMove:
    move = await moves.get_move(session, move_id)
    if move is None or not (move.requested_by == auth.user.id or _holds_view(auth, move)):
        raise ApiError(ErrorCode.MOVE_NOT_FOUND)
    return move


@router.get(MoveRoutes.MOVE_BY_ID.value, response_model=MoveResponse, responses=problems(ErrorCode.MOVE_NOT_FOUND))
async def get_move(
        move_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require()),
):
    """The state of a move: status (waiting, done, cancelled, failed), node, target, requested_at, waiting_seconds,
    and while it waits the uploads below the node: per upload upload_id, post_id, received_bytes, total_bytes,
    last_chunk_at, holding (false: silent for moves.upload_stale_seconds, cancelled at the worker's next look). A
    failed move carries the problem code that refused it in error (the permission checks run again when it runs);
    nothing moved then.

    Needs the permissions a move of the node needs in effect at the node (post.view and post.update for a post; for a
    group also scope_group.view and scope_group.update). The user who asked may always read their move, without the
    uploads once they lost those. Anyone else, and an unknown id: 404 move_not_found."""
    move = await _visible_move(session, auth, move_id)
    return move_response(await moves.view_of(session, move, auth.tree, with_uploads=_holds_view(auth, move)))


@router.post(
    MoveRoutes.MOVE_CANCEL.value,
    response_model=MoveResponse,
    responses=problems(
        ErrorCode.MOVE_NOT_FOUND, ErrorCode.MOVE_NOT_WAITING, AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION,
    ),
)
async def cancel_move(
        request: Request,
        move_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require()),
):
    """Cancel a waiting move: nothing moves, new uploads below the node are accepted at once, running uploads go on
    undisturbed. The user who asked, or anyone who could make the same move (the permissions of the move covering
    where the node sits and where it goes; otherwise 403 permission_required_for_action, action cancel_move, for a
    caller who sees the move). A move that has ended: 409 move_not_waiting with move_status. Visibility as
    GET /moves/{move_id}: 404 move_not_found."""
    move = await _visible_move(session, auth, move_id)
    if move.requested_by != auth.user.id:
        old, new = moves.locations(auth.tree, move)
        for permission, location in move_requirements(MoveNodeKind(move.node_kind), old, new):
            AccessControlService.check_covers(auth, permission, location, "cancel_move")
    try:
        move = await moves.cancel(session, move_id, auth.user.id, client_info(request, auth))
    except CodedError as exc:
        raise ApiError.from_coded(exc)
    return move_response(await moves.view_of(session, move, auth.tree, with_uploads=False))
