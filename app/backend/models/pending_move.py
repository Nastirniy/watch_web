from datetime import datetime
from typing import Optional

from sqlalchemy import Index
from sqlmodel import Field, SQLModel

from app.backend.enums.general import MoveStatus
from app.backend.utils.datetime_utils import get_default_timezone_datetime


class PendingMove(SQLModel, table=True):
    """A move of a post or a group that waits for the uploads below it (services/moves.py).

    The row is the operation: node, destination, who asked. The state is `status` (MoveStatus); a forced move that ran
    over running uploads leaves a row too, done at once, with force true. Moves that run at once without uploads leave
    none. At most one waiting move per node, no two waiting moves whose subtrees overlap, and nothing moves into a
    waiting subtree or takes a target away: decided under the move_locks row (services/moves.py admit).
    """
    __tablename__ = "pending_moves"
    __table_args__ = (
        # the waiting moves (every new upload reads them) and the waiting move of a node
        Index("ix_pending_moves_waiting", "status", "node_kind", "node_id"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    node_kind: str = Field(max_length=8)                    # MoveNodeKind
    node_id: int
    target_group_id: Optional[int] = Field(default=None)    # null: directly under the root
    requested_by: int                                       # no foreign key on actor columns (migration 028)
    requested_at: datetime = Field(default_factory=get_default_timezone_datetime)
    status: str = Field(default=MoveStatus.WAITING.value, max_length=12)
    force: bool = Field(default=False)
    finished_at: Optional[datetime] = Field(default=None)
    error: Optional[str] = Field(default=None, max_length=64)   # the problem code when failed


class MoveLock(SQLModel, table=True):
    """One row (id 1, migration 035), locked as the first statement of every write that moves a node: moves run one
    after another, whatever nodes they touch (services/move_queue.py lock_moves)."""
    __tablename__ = "move_locks"

    id: int = Field(primary_key=True)
