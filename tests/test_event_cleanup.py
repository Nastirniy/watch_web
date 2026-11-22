"""The cleaner of outbox_events: rows past retention go in batches, oldest first, the first row inside retention and
everything after it stay; with every row old the newest stays, so a position always has a row above it"""
from datetime import datetime, timedelta, timezone

from sqlalchemy import insert, select

from app.backend.models.outbox_event import OutboxEvent
from app.backend.services.events.cleanup import clean, oldest_row_age_seconds
from tests.test_media_uploads import storage  # noqa: F401


async def _insert(engine, ids, age_seconds: int) -> None:
    moment = (datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).replace(tzinfo=None)
    async with engine.begin() as connection:
        await connection.execute(insert(OutboxEvent.__table__), [
            {"id": row_id, "type": "scope.changed", "v": 1, "scope_key": "root", "permission": "post.view",
             "payload": "{}", "occurred_at": moment} for row_id in ids])


async def _ids(engine) -> list:
    async with engine.connect() as connection:
        return [row[0] for row in await connection.execute(select(OutboxEvent.__table__.c.id).order_by("id"))]


async def test_old_rows_go_in_batches_and_the_young_stay(engine, session_maker):
    await _insert(engine, range(1, 13), age_seconds=7200)
    await _insert(engine, range(13, 16), age_seconds=10)
    assert await clean(session_maker, retention_seconds=3600, batch=5) == 12
    assert await _ids(engine) == [13, 14, 15]


async def test_when_every_row_is_old_the_newest_stays(engine, session_maker):
    await _insert(engine, range(1, 6), age_seconds=7200)
    assert await clean(session_maker, retention_seconds=3600, batch=2) == 4
    assert await _ids(engine) == [5]
    assert await clean(session_maker, retention_seconds=3600) == 0


async def test_an_empty_table(engine, session_maker):
    assert await clean(session_maker, retention_seconds=3600) == 0
    async with session_maker() as session:
        assert await oldest_row_age_seconds(session) is None


async def test_the_age_of_the_oldest_row(engine, session_maker):
    await _insert(engine, [7], age_seconds=120)
    await _insert(engine, [8], age_seconds=5)
    async with session_maker() as session:
        age = await oldest_row_age_seconds(session)
    assert 115 <= age <= 130


async def test_the_media_worker_cleans_while_events_are_on(engine, session_maker, storage, monkeypatch):
    import asyncio
    from app.backend.config import settings
    from app.backend.workers import media_worker
    from tests.test_queued_moves import _worker
    monkeypatch.setattr(settings.events, "enabled", True)
    monkeypatch.setattr(media_worker, "EVENTS_CLEANUP_SECONDS", 0.01)
    await _insert(engine, range(1, 4), age_seconds=7200)
    worker = _worker(session_maker, storage)
    task = asyncio.create_task(worker._events_cleanup_loop())
    for _ in range(200):
        if await _ids(engine) == [3]:
            break
        await asyncio.sleep(0.01)
    worker._stop.set()
    await asyncio.wait_for(task, 2)
    assert await _ids(engine) == [3]


async def test_a_young_row_below_the_boundary_stays(engine, session_maker, monkeypatch):
    """A late commit: its id lies below the boundary taken before it committed, but it was written just now and the
    hubs may not have read it yet"""
    from app.backend.services.events import cleanup

    async def boundary_before_the_late_commit(session, retention_seconds):
        return 5

    monkeypatch.setattr(cleanup, "_boundary", boundary_before_the_late_commit)
    await _insert(engine, [1, 2], age_seconds=7200)
    await _insert(engine, [3], age_seconds=10)       # the late one
    await _insert(engine, [4], age_seconds=7200)
    await _insert(engine, [5], age_seconds=10)
    assert await clean(session_maker, retention_seconds=3600) == 3
    assert await _ids(engine) == [3, 5]


def test_the_age_of_the_oldest_row_reads_from_the_table_on_mariadb():
    """On MariaDB age_ms is a literal column naming no table: the query must say FROM outbox_events itself (found by
    the load generator: /health answered unhealthy with 1054 on every call once events were on)"""
    from types import SimpleNamespace
    from sqlalchemy.dialects import mysql
    from app.backend.services.events.cleanup import oldest_age_query
    connection = SimpleNamespace(dialect=SimpleNamespace(name="mysql"))
    sql = str(oldest_age_query(connection).compile(dialect=mysql.dialect()))
    assert "FROM outbox_events" in sql
