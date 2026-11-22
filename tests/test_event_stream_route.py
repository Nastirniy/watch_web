"""GET /events/stream through the real app: answers, headers, a real pump tick, replay and reload, no database
connection held, no touch of the session, capacity, disconnect; the v1 messages against their schema."""
import json
from datetime import datetime, timedelta, timezone
from typing import get_args

import pytest
from sqlalchemy import update
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.api.v1.models.response.events import StreamMessage, render_v1
from app.backend.auth.scope import Node, ROOT
from app.backend.auth.service import AuthService
from app.backend.auth.sessions import utc
from app.backend.config import EventsConfig, settings
from app.backend.enums.events import EventType, StreamCloseReason
from app.backend.enums.general import Permissions as P
from app.backend.enums.routes import EventRoutes
from app.backend.models.user import UserSession
from app.backend.services.events import publish
from app.backend.services.events.hub import EventHub
from app.backend.services.events.reader import OutboxRow
from app.main import fastapi_app
from tests.helpers import give, make_admin, make_user, open_stream

STREAM = V1.path(EventRoutes.STREAM)


def blocks(data: bytes) -> list:
    """(fields, json) of each block"""
    result = []
    for text in data.decode().split("\n\n"):
        if text:
            fields = dict(line.split(": ", 1) for line in text.split("\n"))
            result.append((fields, json.loads(fields["data"])))
    return result


def types(data: bytes) -> list:
    return [value["type"] for _, value in blocks(data)]


@pytest.fixture
async def hub(engine, monkeypatch):
    monkeypatch.setattr(settings.events, "enabled", True)
    hub = EventHub(engine, render_v1, EventsConfig(enabled=True), workers=4)
    await hub.start(run_tasks=False)
    fastapi_app.state.event_hub = hub
    yield hub
    fastapi_app.state.event_hub = None


async def test_off_answers_204_and_without_a_token_401(client, db, headers):
    user = await make_user(db)
    fastapi_app.state.event_hub = None
    answer = await client.get(STREAM, headers=headers(user))
    assert answer.status_code == 204 and answer.content == b""
    refused = await client.get(STREAM)
    assert refused.status_code == 401 and refused.headers["content-type"].startswith("application/problem+json")


async def test_the_first_bytes_and_the_headers(client, db, headers, hub):
    user = await make_user(db)
    stream = await open_stream(fastapi_app, STREAM, headers(user))
    assert stream.status == 200
    assert stream.response_headers["content-type"] == "text/event-stream; charset=utf-8"
    assert stream.response_headers["cache-control"] == "no-store"
    assert stream.response_headers["x-accel-buffering"] == "no"
    first = await stream.chunk()
    fields, value = blocks(first)[0]
    assert value == {"type": "ready"} and fields["event"] == "ready" and fields["id"].isdigit()
    assert settings.events.retry_min_ms <= int(fields["retry"]) <= settings.events.retry_max_ms
    await stream.disconnect()


async def test_a_published_row_arrives_through_a_pump_tick(client, db, headers, hub):
    user = await make_admin(db)
    stream = await open_stream(fastapi_app, STREAM, headers(user))
    await stream.chunk()
    publish(db, EventType.MOVE_CHANGED, scope=Node.post(7), permission=P.POST_UPDATE, move_id=5, move_status="waiting")
    await db.commit()
    await hub.tick()
    fields, value = blocks(await stream.chunk())[0]
    assert fields["event"] == "move.changed" and int(fields["id"]) == hub.position
    StreamMessage.model_validate(value)
    assert value["data"] == {"move_id": 5, "move_status": "waiting"} and value["scope"] == {"kind": "post", "id": 7}
    assert value["refetch_within_ms"] == 1 * 4 * 1000 // 30
    await stream.disconnect()


@pytest.mark.parametrize("sent", ["abc", "999999999999", "1"])
async def test_a_position_we_cannot_answer_for_gives_reload(client, db, headers, hub, monkeypatch, sent):
    monkeypatch.setattr(hub.ring, "floor", 5)        # "1" lies before the ring
    hub.gaps.head = hub.position = max(hub.gaps.head, 5)
    user = await make_user(db)
    stream = await open_stream(fastapi_app, STREAM, headers(user), last_event_id=sent)
    assert stream.status == 200
    assert types(await stream.chunk()) == ["ready", "reload"]
    await stream.disconnect()


async def test_a_reconnect_replays_what_it_missed(client, db, headers, hub):
    user = await make_admin(db)
    start = hub.position
    for move_id in (1, 2):
        publish(db, EventType.MOVE_CHANGED, scope=ROOT, permission=P.POST_UPDATE, move_id=move_id, move_status="done")
    await db.commit()
    await hub.tick()
    stream = await open_stream(fastapi_app, STREAM, headers(user), last_event_id=str(start))
    replayed = blocks(await stream.chunk())
    assert [value["type"] for _, value in replayed] == ["ready", "move.changed", "move.changed"]
    assert {fields["id"] for fields, _ in replayed} == {str(hub.position)}
    await stream.disconnect()


async def test_the_stream_holds_no_database_connection(client, db, headers, hub, monkeypatch):
    from sqlmodel.ext.asyncio.session import AsyncSession
    closed = []
    original = AsyncSession.close

    async def spy(self):
        closed.append(self)
        await original(self)

    monkeypatch.setattr(AsyncSession, "close", spy)
    user = await make_user(db)
    stream = await open_stream(fastapi_app, STREAM, headers(user))
    assert closed, "the request session was closed before the first chunk"
    await stream.chunk()
    await stream.disconnect()


async def test_a_stream_does_not_touch_the_session(client, db, headers, hub, session_maker):
    user = await make_user(db)
    token = headers(user)
    claims = AuthService.verify_token(token["Authorization"].split(" ", 1)[1], "access")
    async with session_maker():     # stores the login session
        pass
    then = datetime.now(timezone.utc) - timedelta(minutes=5)
    await db.execute(update(UserSession).where(UserSession.id == claims["sid"])
                     .values(last_activity=then, expires_at=then + timedelta(minutes=30)))
    await db.commit()
    stream = await open_stream(fastapi_app, STREAM, token)
    await stream.chunk()
    await stream.disconnect()
    async with session_maker() as fresh:
        stored = (await fresh.exec(select(UserSession).where(UserSession.id == claims["sid"]))).one()
    assert abs(utc(stored.last_activity) - then) < timedelta(seconds=1)


@pytest.mark.parametrize("state", ["full", "stopping"])
async def test_503_when_full_or_stopping(client, db, headers, hub, monkeypatch, state):
    if state == "full":
        monkeypatch.setattr(hub.config, "max_streams_per_worker", 1)
        first = await open_stream(fastapi_app, STREAM, headers(await make_user(db)))
    else:
        hub.drain()
    answer = await client.get(STREAM, headers=headers(await make_user(db)))
    assert answer.status_code == 503 and answer.json()["code"] == "event_stream_capacity"
    assert 5 <= int(answer.headers["Retry-After"]) <= 30
    if state == "full":
        await first.disconnect()


async def test_disconnect_detaches(client, db, headers, hub):
    user = await make_user(db)
    stream = await open_stream(fastapi_app, STREAM, headers(user))
    await stream.chunk()
    assert len(hub.connections) == 1
    await stream.disconnect()
    assert hub.connections == set() and hub.by_user == {}


async def test_a_close_ends_the_response(client, db, headers, hub):
    user = await make_user(db)
    stream = await open_stream(fastapi_app, STREAM, headers(user))
    await stream.chunk()
    hub.drain()
    rest = await stream.body()
    fields, value = blocks(rest)[-1]
    assert value == {"type": "close", "data": {"reason": "restart"}} and int(fields["retry"]) >= 1000
    assert stream.ended and hub.connections == set()
    await stream.disconnect()


async def test_the_route_is_in_route_access(client, db, headers):
    from app.backend.auth.route_access import route_access_map
    entries = [entry for entry in route_access_map(fastapi_app) if entry.path == STREAM]
    assert entries and not entries[0].require.all and not entries[0].require.any


# ---------------------------------------------------------------- the v1 messages

def _row(**overrides) -> OutboxRow:
    values = {"id": 41, "type": "move.changed", "v": 1, "scope_key": "g:3", "permission": "post.update",
              "user_id": None, "conflate_key": None, "payload": '{"move_id":9,"move_status":"cancelled"}',
              "occurred_at": datetime(2026, 11, 22, 10, 0, 0, 123456), "lag_ms": 0}
    return OutboxRow(**{**values, **overrides})


def _value(body: bytes) -> dict:
    head, data, end = body.split(b"\n", 2)
    assert head.startswith(b"event: ") and data.startswith(b"data: ") and end == b"\n"
    return json.loads(data[len(b"data: "):])


@pytest.mark.parametrize("row", [
    _row(),
    _row(scope_key="root"),
    _row(scope_key=None, user_id=3),
    _row(type="scope.changed", payload="{}", scope_key="p:7", occurred_at=datetime(2026, 11, 22, 10, 0, 0)),
    _row(type="security.certificate_mismatch", payload='{"audit_log_id":12,"session_user_id":5}', scope_key="root",
         permission="audit.view"),
    _row(type="user.changed", payload='{"subject_user_id":5}', scope_key="p:7", permission="user.view", user_id=5),
    _row(type="certificate.changed", payload='{"subject_user_id":5,"certificate_id":9}', scope_key="root",
         permission="certificate.view", user_id=5),
    _row(type="session.changed", payload='{"subject_user_id":5}', scope_key="p:7", permission="user.view", user_id=5),
    _row(type="role.changed", payload='{"role_id":3}', scope_key="*", permission="role.view"),
    _row(type="dictionary.changed", payload='{"dictionary":"guides"}', scope_key="*", permission=None),
])
def test_every_rendered_event_validates(row):
    value = _value(render_v1.event(row, 1234))
    message = StreamMessage.model_validate(value).root
    assert message.type == row.type and value["id"] == str(row.id) and value["refetch_within_ms"] == 1234
    assert value["occurred_at"].endswith("Z")
    assert ("scope" in value) == (row.scope_key not in (None, "*")), "absent, never null; no place for a star"
    if row.scope_key == "root":
        assert value["scope"] == {"kind": "root"}


def test_every_stream_message_validates():
    for body in (render_v1.ready(), render_v1.heartbeat(), render_v1.reload(500),
                 *(render_v1.close(reason) for reason in StreamCloseReason)):
        StreamMessage.model_validate(_value(body))


def test_every_event_type_has_a_member_in_the_union():
    members = get_args(get_args(StreamMessage.model_fields["root"].annotation)[0])
    declared = {get_args(member.model_fields["type"].annotation)[0] for member in members}
    assert {event.value for event in EventType} <= declared


# ---------------------------------------------------------------- the lifespan

async def test_serve_streams_false_runs_no_hub_and_the_route_answers_204(client, db, headers, monkeypatch):
    import app.main as main
    monkeypatch.setattr(settings.events, "enabled", True)
    monkeypatch.setattr(settings.events, "serve_streams", False)

    async def no_engine():
        raise AssertionError("no hub, no engine")

    monkeypatch.setattr(main, "create_events_engine", no_engine)
    await main.start_event_hub(fastapi_app)
    assert fastapi_app.state.event_hub is None
    answer = await client.get(STREAM, headers=headers(await make_user(db)))
    assert answer.status_code == 204


async def test_the_lifespan_starts_and_stops_the_hub(client, db, monkeypatch):
    import signal
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import StaticPool
    from sqlmodel import SQLModel
    import app.main as main
    monkeypatch.setattr(settings.events, "enabled", True)
    own = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    async with own.begin() as connection:
        await connection.run_sync(SQLModel.metadata.create_all)

    async def hub_engine():
        return own

    monkeypatch.setattr(main, "create_events_engine", hub_engine)
    previous = signal.getsignal(signal.SIGTERM)
    try:
        await main.start_event_hub(fastapi_app)
        hub = fastapi_app.state.event_hub
        assert hub is not None and hub.started and len(hub.tasks) == 5
        if hasattr(signal, "SIGTERM"):
            assert signal.getsignal(signal.SIGTERM) is not previous, "the drain is chained in front"
        tasks = list(hub.tasks)
        await main.stop_event_hub(fastapi_app)
        assert fastapi_app.state.event_hub is None and hub.stopping and all(task.done() for task in tasks)
    finally:
        signal.signal(signal.SIGTERM, previous)
