"""Rows of outbox_events past events.retention_seconds, removed by the media worker every 60 s (the only delete of
the table outside a migration and the carry-over of a restore). Events are delivery data, never a record.

clean() takes the boundary first, the first row inside retention (a walk over the old rows only, by the primary key),
or the newest row when every row is old: that row stays, so AUTO_INCREMENT and any position have a row above them.
Then batches below the boundary, oldest first, each its own short transaction: the ids of one batch by the primary
key, then a DELETE of exactly those ids as the first statement of its own transaction. Never a range to the end of
the index and never an open transaction: the
plan of a range DELETE flips between range and full scan with the row count (measured), and a full scan held open
blocks every insert at the end of the index. Two media workers are safe: a batch the other removed first deletes
nothing, and the next run tries again.
"""
from typing import Optional

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.backend.models.outbox_event import OutboxEvent
from app.backend.services.events.reader import age_ms, cutoff

T = OutboxEvent.__table__.c


async def _boundary(session, retention_seconds: int) -> int:
    connection = await session.connection()
    first = (await session.execute(
        select(T.id).where(T.occurred_at >= cutoff(connection, retention_seconds)).order_by(T.id).limit(1))).scalar()
    if first is None:
        first = (await session.execute(select(func.coalesce(func.max(T.id), 0)))).scalar()
    await session.commit()
    return int(first or 0)


async def clean(session_maker: async_sessionmaker, retention_seconds: int, batch: int = 500) -> int:
    """Remove the rows past retention; the number removed"""
    removed = 0
    async with session_maker() as session:
        boundary = await _boundary(session, retention_seconds)
        while True:
            # occurred_at too: a late commit below the boundary is young, and the hubs may not have read it yet
            connection = await session.connection()
            ids = [int(row_id) for row_id in (await session.execute(
                select(T.id).where(T.id < boundary, T.occurred_at < cutoff(connection, retention_seconds))
                .order_by(T.id).limit(batch))).scalars()]
            # The read ends before the DELETE: a DELETE after a plain read in one transaction answers 1020 under
            # snapshot isolation while writers insert (measured, MariaDB 11.8.9); as the first statement it cannot
            await session.commit()
            if not ids:
                return removed
            result = await session.execute(delete(OutboxEvent.__table__).where(T.id.in_(ids)))
            await session.commit()
            removed += result.rowcount or 0
            if len(ids) < batch:
                return removed


async def oldest_row_age_seconds(session) -> Optional[int]:
    """Seconds since the oldest row was written, by the database clock; None for an empty table (/health)"""
    connection = await session.connection()
    age = (await session.execute(select(age_ms(connection)).order_by(T.id).limit(1))).scalar()
    return None if age is None else max(0, int(age)) // 1000
