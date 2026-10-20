"""Write transactions that begin with their row locks.

MariaDB 11.6+ (innodb_snapshot_isolation, on by default) refuses a write to, or a locking read of, a row that
changed after the transaction's snapshot was taken: error 1020, "Record has changed since last read". The snapshot is
taken by the first plain read of a transaction, and a request reads before its handler runs (authentication, the
auth context), so a write that locked its row afterwards lost every race with a 500 (live race test, 2026-10-19).

lock_first ends that read-only transaction and makes the lock the first statement of a new one. A losing request
then waits for the winner, reads its committed result, and answers from it like any other request (404 already
archived, 409 already revoked). Later locks of the same write use lock. A rule that spans rows (the last
assignment of a user, the last administrator) holds only when every write that can break it takes the same lock
first: the plain reads that check it then start after the previous writer committed.

MySQL takes the same path (it never raises 1020, the locks serialise the writes there as well).
"""
from typing import List, Optional, TypeVar

from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel.sql.expression import SelectOfScalar

T = TypeVar("T")


async def start_write(session: AsyncSession) -> None:
    """End the transaction the request's reads opened, so that the next statement starts a new one. Only before
    the first change of the request: committing a session with changes would commit half of a write."""
    if session.new or session.dirty or session.deleted:
        raise RuntimeError("start_write after a change of the request: it would commit half of the write")
    if session.in_transaction():
        await session.commit()


def locking(query: SelectOfScalar[T], shared: bool = False) -> SelectOfScalar[T]:
    """query as a locking read of the committed rows: FOR UPDATE, or with shared=True a shared lock (LOCK IN SHARE
    MODE on MariaDB): readers of the row that must see it unchanged until they commit, side by side"""
    return query.with_for_update(read=shared).execution_options(populate_existing=True)


async def lock(session: AsyncSession, query: SelectOfScalar[T], shared: bool = False) -> Optional[T]:
    """The first row of query, locked (FOR UPDATE, or shared) and as committed: the copy the session holds from an
    earlier read is overwritten (populate_existing). Inside a write that began with lock_first."""
    return (await session.exec(locking(query, shared))).first()


async def lock_all(session: AsyncSession, query: SelectOfScalar[T]) -> List[T]:
    """Every row of query, locked (FOR UPDATE) and as committed; give the query an ORDER BY id so that every writer
    locks the rows in the same order. Inside a write that began with lock_first."""
    return list((await session.exec(query.with_for_update().execution_options(populate_existing=True))).all())


async def lock_first(session: AsyncSession, query: SelectOfScalar[T], shared: bool = False) -> Optional[T]:
    """The first statement of a write transaction: the row of query locked and as committed (see the module)"""
    await start_write(session)
    return await lock(session, query, shared)
