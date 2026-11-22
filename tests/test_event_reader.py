"""The reader of the outbox on SQLite: rows out of order by id come once each, a gap filled later comes through the
statement of open ids, the start position knows the last minute. The races themselves were run on MariaDB 11.8."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import insert

from app.backend.models.outbox_event import OutboxEvent
from app.backend.services.events.gaps import GapTracker
from app.backend.services.events.reader import first_head, new_rows_of, read_tick


def tracker(head: int = 0) -> GapTracker:
    return GapTracker(ttl=60, hold=8, max_jump=1000, max_gaps=5000, head=head)


async def _insert(engine, *ids: int, age_seconds: int = 0) -> None:
    values = [{"id": row_id, "type": "scope.changed", "v": 1, "scope_key": "root", "permission": "post.view",
               "payload": "{}"} for row_id in ids]
    if age_seconds:
        moment = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).replace(tzinfo=None)
        for value in values:
            value["occurred_at"] = moment
    async with engine.begin() as connection:
        await connection.execute(insert(OutboxEvent.__table__), values)


async def _tick(engine, gaps: GapTracker, batch: int = 1000) -> list:
    rows = await read_tick(engine, gaps, batch)
    gaps.saw((row.id for row in rows), now=0)
    return [row.id for row in rows]


async def test_rows_in_order_and_once(engine):
    gaps = tracker()
    await _insert(engine, 1, 2, 3)
    assert await _tick(engine, gaps) == [1, 2, 3]
    assert await _tick(engine, gaps) == []


async def test_a_late_commit_arrives_through_the_open_ids(engine):
    gaps = tracker()
    await _insert(engine, 1, 3)                     # 2 is taken, not committed yet
    assert await _tick(engine, gaps) == [1, 3]
    assert gaps.open_ids() == [2]
    await _insert(engine, 2, 4)
    assert await _tick(engine, gaps) == [2, 4], "the late row first, then the new one"
    assert gaps.open_ids() == [] and await _tick(engine, gaps) == []


async def test_a_full_batch_reads_again(engine):
    gaps = tracker()
    await _insert(engine, *range(1, 8))
    rows = await read_tick(engine, gaps, batch=5)
    assert new_rows_of(rows, gaps.head) == 5
    gaps.saw((row.id for row in rows), now=0)
    assert await _tick(engine, gaps, batch=5) == [6, 7]


async def test_rows_carry_their_fields_and_a_lag(engine):
    await _insert(engine, 1)
    (row,) = await read_tick(engine, tracker())
    assert (row.type, row.v, row.scope_key, row.permission, row.user_id, row.payload) == (
        "scope.changed", 1, "root", "post.view", None, "{}")
    assert row.occurred_at is not None and 0 <= row.lag_ms < 60000


async def test_first_head_of_an_empty_table_is_zero(engine):
    assert await first_head(engine, 60) == 0


async def test_first_head_keeps_the_last_minute(engine):
    await _insert(engine, 1, 2, age_seconds=600)
    await _insert(engine, 3, 5)
    assert await first_head(engine, 60) == 2
    gaps = tracker(head=2)
    assert await _tick(engine, gaps) == [3, 5] and gaps.open_ids() == [4]


async def test_first_head_when_every_row_is_older(engine):
    await _insert(engine, 1, 2, age_seconds=600)
    assert await first_head(engine, 60) == 2
