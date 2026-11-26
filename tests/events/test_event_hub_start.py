"""A hub that fails to start never stops the API: the error is logged once with its traceback, the stream route answers
503 event_stream_capacity with a long Retry-After, /health counts the worker as down, and the start is retried in the
background (5 s doubling to 5 min) until it works; then streams are served, with the SIGTERM drain."""
import asyncio
import json
import signal

import pytest
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncEngine

from app.backend.api.v1 import V1
from app.backend.config import settings
from app.backend.enums.routes import EventRoutes, SystemRoutes
from app.backend.models.worker import WorkerHeartbeat
from app.backend.services.events import starter as starter_module
from app.backend.services.events.hub import hub_name
from app.backend.services.events.starter import HubStarter
from app.main import fastapi_app
from tests.helpers import make_user, open_stream

STREAM = V1.path(EventRoutes.STREAM)


class FakeHub:
    def __init__(self):
        self.stopped = False

    async def stop(self):
        self.stopped = True


class Script:
    """make_hub: fails `failures` times (with the given exceptions, cycled), then returns a FakeHub"""

    def __init__(self, failures: int, errors=(RuntimeError("no database"),)):
        self.failures, self.errors, self.calls = failures, errors, 0
        self.hub = FakeHub()

    async def __call__(self):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.errors[(self.calls - 1) % len(self.errors)]
        return self.hub


def recorder():
    slept = []

    async def sleep(seconds):
        slept.append(seconds)
        await asyncio.sleep(0)

    return slept, sleep


@pytest.fixture
def lines():
    captured = []
    sink = logger.add(lambda message: captured.append(message.record), level="INFO", format="{message}")
    yield captured
    logger.remove(sink)


def _starter(make_hub, session_maker, sleep, started=None, **kwargs):
    async def sessions():
        return session_maker

    return HubStarter(make_hub, (started.append if started is not None else lambda hub: None), sessions,
                      sleep=sleep, **kwargs)


async def _until(condition, seconds: float = 5.0):
    for _ in range(int(seconds / 0.01)):
        if condition():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out")


async def test_a_first_start_that_works_runs_no_loop(db, session_maker):
    slept, sleep = recorder()
    started = []
    script = Script(0)
    starter = _starter(script, session_maker, sleep, started)
    assert await starter.start() is script.hub
    assert started == [script.hub] and starter.task is None and slept == []
    await starter.stop()
    assert script.hub.stopped


async def test_the_retry_backs_off_from_5_s_doubling_to_5_min(db, session_maker):
    slept, sleep = recorder()
    started = []
    script = Script(9)
    starter = _starter(script, session_maker, sleep, started, report_seconds=1000)
    assert await starter.start() is None and started == []
    await _until(lambda: started)
    assert slept == [5, 10, 20, 40, 80, 160, 300, 300, 300]
    assert started == [script.hub] and starter.hub is script.hub and script.calls == 10
    await starter.stop()
    assert script.hub.stopped


async def test_the_down_row_is_written_at_once_and_kept_fresh_while_waiting(db, session_maker):
    """/health reads rows within 60 s: a wait of 5 min refreshes the row every report interval"""
    gate = asyncio.Event()
    slept = []

    async def sleep(seconds):
        slept.append(seconds)
        if len(slept) >= 3:
            await gate.wait()

    starter = _starter(Script(100), session_maker, sleep, report_seconds=2, first_delay=5)
    await starter.start()
    row = await db.get(WorkerHeartbeat, hub_name())
    assert row is not None and row.kind == "events" and json.loads(row.details) == {"down": 1}
    await _until(lambda: len(slept) >= 3)
    assert slept == [2, 2, 1], "a wait of 5 s in report intervals of 2 s"
    await starter.stop()
    db.expire_all()
    assert await db.get(WorkerHeartbeat, hub_name()) is None, "a stop removes the down row"


async def test_the_error_is_logged_once_with_its_traceback(db, session_maker, lines):
    slept, sleep = recorder()
    started = []
    script = Script(4, errors=(RuntimeError("no database"), RuntimeError("no database"), RuntimeError("again"),
                               OSError("other")))
    starter = _starter(script, session_maker, sleep, started)
    await starter.start()
    await _until(lambda: started)
    with_traceback = [record for record in lines if record["exception"] is not None]
    assert [type(record["exception"].value).__name__ for record in with_traceback] == ["RuntimeError", "OSError"], \
        "a traceback for the first failure and for a failure of another kind, never for a repeat"
    assert sum("failed to start" in record["message"] for record in lines) == 4
    assert any("started" in record["message"] and "attempt" in record["message"] for record in lines)
    await starter.stop()


async def test_a_stop_cancels_the_loop_while_it_waits(db, session_maker):
    forever = asyncio.Event()

    async def sleep(seconds):
        await forever.wait()

    script = Script(100)
    starter = _starter(script, session_maker, sleep)
    await starter.start()
    task = starter.task
    await asyncio.sleep(0)
    await starter.stop()
    assert task.done() and starter.task is None and script.calls == 1


async def test_a_stop_while_a_start_runs_cancels_it(db, session_maker):
    entered, never = asyncio.Event(), asyncio.Event()
    calls = []

    async def make_hub():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("first")
        entered.set()
        await never.wait()

    slept, sleep = recorder()
    starter = _starter(make_hub, session_maker, sleep)
    await starter.start()
    await entered.wait()
    await starter.stop()
    assert starter.task is None and starter.hub is None


# ---------------------------------------------------------------- through the app


async def test_the_api_starts_without_a_hub_answers_503_then_serves_when_it_starts(
        client, db, engine, headers, session_maker, monkeypatch):
    """The hub runs on the test database (its reports reach /health); its stop disposes its engine, which here is
    the test's own: recorded instead"""
    import app.main as main
    monkeypatch.setattr(settings.events, "enabled", True)
    monkeypatch.setattr(settings.health, "enable_rate_limit", False)
    monkeypatch.setattr(starter_module, "RETRY_FIRST_SECONDS", 0.05)
    disposed = []

    async def dispose(self, close=True):
        disposed.append(self)

    monkeypatch.setattr(AsyncEngine, "dispose", dispose)
    attempts = []
    allowed = asyncio.Event()

    async def events_engine():
        attempts.append(1)
        if not allowed.is_set():
            raise ConnectionRefusedError("the database is not there yet")
        return engine

    async def sessions():
        return session_maker

    monkeypatch.setattr(main, "create_events_engine", events_engine)
    monkeypatch.setattr(main, "get_session_maker", sessions)
    previous = signal.getsignal(signal.SIGTERM)
    user = await make_user(db)
    try:
        await main.start_event_hub(fastapi_app)          # no exception: the API starts
        assert fastapi_app.state.event_hub is None

        refused = await client.get(STREAM, headers=headers(user))
        assert refused.status_code == 503 and refused.json()["code"] == "event_stream_capacity", refused.text
        assert 30 <= int(refused.headers["retry-after"]) <= 120

        health = (await client.get(SystemRoutes.HEALTH.value)).json()
        assert health["events"]["workers_down"] == 1 and health["events"]["workers"] == 0
        assert health["events"]["status"] == "degraded" and health["status"] == "degraded"
        assert signal.getsignal(signal.SIGTERM) is previous, "no drain without a hub"

        allowed.set()
        await _until(lambda: fastapi_app.state.event_hub is not None)
        hub = fastapi_app.state.event_hub
        assert hub.started and len(hub.tasks) == 5 and len(attempts) >= 2
        if hasattr(signal, "SIGTERM"):
            assert signal.getsignal(signal.SIGTERM) is not previous, "the drain is chained in when the hub starts late"

        stream = await open_stream(fastapi_app, STREAM, headers(user))
        assert stream.status == 200
        await stream.chunk()
        await stream.disconnect()

        health = (await client.get(SystemRoutes.HEALTH.value)).json()
        assert health["events"]["workers_down"] == 0 and health["events"]["workers"] == 1

        tasks = list(hub.tasks)
        await main.stop_event_hub(fastapi_app)
        assert fastapi_app.state.event_hub is None and hub.stopping and all(task.done() for task in tasks)
        assert disposed == [engine]
    finally:
        signal.signal(signal.SIGTERM, previous)
        await main.stop_event_hub(fastapi_app)


async def test_a_shutdown_while_the_hub_is_down_cancels_the_retry(client, db, session_maker, monkeypatch):
    import app.main as main
    monkeypatch.setattr(settings.events, "enabled", True)

    async def events_engine():
        raise ConnectionRefusedError("never")

    async def sessions():
        return session_maker

    monkeypatch.setattr(main, "create_events_engine", events_engine)
    monkeypatch.setattr(main, "get_session_maker", sessions)
    await main.start_event_hub(fastapi_app)
    task = fastapi_app.state.event_hub_starter.task
    assert task is not None and not task.done()
    await main.stop_event_hub(fastapi_app)
    assert task.done() and fastapi_app.state.event_hub is None


async def test_204_stays_for_events_off_and_for_a_process_that_serves_no_streams(client, db, headers, monkeypatch):
    user = await make_user(db)
    fastapi_app.state.event_hub = None
    assert (await client.get(STREAM, headers=headers(user))).status_code == 204          # events off
    monkeypatch.setattr(settings.events, "enabled", True)
    monkeypatch.setattr(settings.events, "serve_streams", False)
    assert (await client.get(STREAM, headers=headers(user))).status_code == 204
