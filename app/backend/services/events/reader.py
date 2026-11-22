"""The reads of a hub, on its own engine (never the request pool).

A tick is two statements in one short transaction, ended at once: a poller in one open transaction would see nothing
new (proved). The new rows above the head, and the ids the gap tracker still watches (late commits). Times come from
the database clock only, in UTC: the rows carry it (occurred_at), and the lag and the retention boundary are computed
there.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import List, Optional, Sequence

from sqlalchemy import Integer, bindparam, cast, func, literal_column, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine
from sqlalchemy.sql.elements import ColumnElement

from app.backend.models.outbox_event import OutboxEvent
from app.backend.services.events.gaps import GapTracker

T = OutboxEvent.__table__.c


@dataclass(frozen=True)
class OutboxRow:
    id: int
    type: str
    v: int
    scope_key: Optional[str]
    permission: Optional[str]
    user_id: Optional[int]
    conflate_key: Optional[str]
    payload: str
    occurred_at: datetime
    lag_ms: int          # read time minus occurred_at, both from the database clock


def _is_sqlite(connection: AsyncConnection) -> bool:
    return connection.dialect.name == "sqlite"


def age_ms(connection: AsyncConnection) -> ColumnElement:
    """Milliseconds since occurred_at, by the database clock (UTC)"""
    if _is_sqlite(connection):
        return cast((func.julianday("now") - func.julianday(T.occurred_at)) * 86400000, Integer)
    return literal_column("TIMESTAMPDIFF(MICROSECOND, occurred_at, UTC_TIMESTAMP(6)) DIV 1000")


def cutoff(connection: AsyncConnection, seconds: int) -> ColumnElement:
    """The database clock (UTC) minus seconds, comparable with occurred_at"""
    if _is_sqlite(connection):
        return func.datetime("now", f"-{int(seconds)} seconds")
    return text("UTC_TIMESTAMP(6) - INTERVAL :cutoff_seconds SECOND").bindparams(cutoff_seconds=int(seconds))


def _columns(connection: AsyncConnection):
    return (T.id, T.type, T.v, T.scope_key, T.permission, T.user_id, T.conflate_key, T.payload, T.occurred_at,
            age_ms(connection).label("lag_ms"))


def _row(row) -> OutboxRow:
    # MariaDB answers an unsigned BIGINT expression as a Decimal: ids and numbers are cast
    return OutboxRow(id=int(row.id), type=row.type, v=int(row.v), scope_key=row.scope_key, permission=row.permission,
                     user_id=int(row.user_id) if row.user_id is not None else None, conflate_key=row.conflate_key,
                     payload=row.payload, occurred_at=row.occurred_at, lag_ms=max(0, int(row.lag_ms or 0)))


async def read_rows(connection: AsyncConnection, head: int, open_ids: Sequence[int], batch: int) -> List[OutboxRow]:
    """The late rows (ids still watched) and then the new ones above head, oldest first"""
    late: List[OutboxRow] = []
    if open_ids:
        query = select(*_columns(connection)).where(T.id.in_(bindparam("open_ids", expanding=True))).order_by(T.id)
        late = [_row(row) for row in await connection.execute(query, {"open_ids": list(open_ids)})]
    query = select(*_columns(connection)).where(T.id > head).order_by(T.id).limit(batch)
    return late + [_row(row) for row in await connection.execute(query)]


async def read_tick(engine: AsyncEngine, tracker: GapTracker, batch: int = 1000) -> List[OutboxRow]:
    """One tick of the pump: late + new, in one short transaction ended at once. A batch of new rows that is full means
    more wait: the caller reads again at once (new_rows_of below)."""
    async with engine.connect() as connection:
        try:
            return await read_rows(connection, tracker.head, tracker.open_ids(), batch)
        finally:
            await connection.rollback()


def new_rows_of(rows: Sequence[OutboxRow], head: int) -> int:
    """How many of the rows of a tick lie above the head it was read with: a full batch means read again"""
    return sum(1 for row in rows if row.id > head)


async def first_head(engine: AsyncEngine, seconds: int) -> int:
    """Where a worker that starts begins: the newest row older than seconds, so every id above it is read or watched
    as a gap, a transaction still in flight below the first young visible row included, and the ring is filled with
    the last minute. 0 when no row is that old (an empty table, or only young rows: all of them are read)."""
    async with engine.connect() as connection:
        try:
            last_old = (await connection.execute(
                select(func.max(T.id)).where(T.occurred_at < cutoff(connection, seconds)))).scalar()
            return int(last_old or 0)
        finally:
            await connection.rollback()
