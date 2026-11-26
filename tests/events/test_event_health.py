"""/health: the events part, summed from the rows the hubs write (a request sees one worker only)"""
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.backend.api.v1.models.response.events import render_v1
from app.backend.config import EventsConfig, settings
from app.backend.enums.routes import SystemRoutes
from app.backend.models.worker import WorkerHeartbeat
from app.backend.services.events.hub import EventHub


@pytest.fixture(autouse=True)
def no_rate_limit(monkeypatch):
    monkeypatch.setattr(settings.health, "enable_rate_limit", False)


async def health(client) -> dict:
    response = await client.get(SystemRoutes.HEALTH.value)
    assert response.status_code == 200, response.text
    return response.json()


def _row(name: str, seconds_ago: int, **counts) -> WorkerHeartbeat:
    seen = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
    return WorkerHeartbeat(name=name, kind="events", hostname="h", pid=1, started_at=seen, last_seen_at=seen,
                           details=json.dumps(counts))


async def test_absent_while_events_are_off(client, db):
    assert (await health(client)).get("events") is None


async def test_no_hub_reported_is_degraded(client, db, monkeypatch):
    monkeypatch.setattr(settings.events, "enabled", True)
    body = await health(client)
    assert body["events"]["status"] == "degraded" and body["events"]["workers"] == 0
    assert body["status"] == "degraded"


async def test_the_fresh_reports_are_summed(client, db, monkeypatch):
    monkeypatch.setattr(settings.events, "enabled", True)
    db.add(_row("events@a:1", 5, streams=10, lag_ms=400, gaps=1, slow_closed=2, reloads=3))
    db.add(_row("events@a:2", 10, streams=5, lag_ms=900, gaps=0, slow_closed=0, reloads=1))
    db.add(_row("events@a:3", 600, streams=99, lag_ms=99999))          # a worker gone long ago
    db.add(WorkerHeartbeat(name="media@a:4", kind="media", hostname="h", pid=4, details="running=0"))
    await db.commit()
    events = (await health(client))["events"]
    assert events == {"status": "healthy", "workers": 2, "workers_down": 0, "streams": 15, "lag_ms": 900, "gaps": 1,
                      "slow_closed": 2, "reloads": 4, "oldest_row_age_seconds": None}


async def test_a_worker_whose_hub_is_down_is_counted_and_degraded(client, db, monkeypatch):
    monkeypatch.setattr(settings.events, "enabled", True)
    db.add(_row("events@a:1", 5, streams=10, lag_ms=400))
    db.add(_row("events@a:2", 5, down=1))
    db.add(_row("events@a:3", 600, down=1))                            # long gone
    await db.commit()
    body = await health(client)
    assert {key: body["events"][key] for key in ("status", "workers", "workers_down", "streams")} == {
        "status": "degraded", "workers": 1, "workers_down": 1, "streams": 10}
    assert body["status"] == "degraded"


async def test_a_lag_above_30_s_is_degraded(client, db, monkeypatch):
    monkeypatch.setattr(settings.events, "enabled", True)
    db.add(_row("events@a:1", 5, streams=1, lag_ms=31000))
    await db.commit()
    assert (await health(client))["events"]["status"] == "degraded"


async def test_a_hub_reports_its_counts(engine, db):
    hub = EventHub(engine, render_v1, EventsConfig(enabled=True), workers=4)
    await hub.start(run_tasks=False)
    await hub.report()
    row = await db.get(WorkerHeartbeat, hub.name)
    assert row.kind == "events" and len(row.details) <= 500
    assert json.loads(row.details) == {"streams": 0, "lag_ms": 0, "gaps": 0, "slow_closed": 0, "reloads": 0}
