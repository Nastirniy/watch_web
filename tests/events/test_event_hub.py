"""The hub without sockets: a real SQLite outbox, real pump ticks, a renderer of its own"""
import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from app.backend.auth.context import AuthContext, load_auth_context
from app.backend.auth.scope import Grant, Node, ROOT
from app.backend.config import EventsConfig, settings
from app.backend.enums.events import EventType, StreamCloseReason
from app.backend.enums.general import Permissions as P, UserType
from app.backend.models.user import User, UserSession
from app.backend.services.events import publish
from app.backend.services.events.hub import EventHub
from tests.helpers import give, make_group, make_post, make_user


class TextRenderer:
    """event: <type>\\ndata: <json>\\n\\n with the fields the tests look at"""

    def _block(self, kind: str, data: dict) -> bytes:
        return b"event: %s\ndata: %s\n\n" % (kind.encode(), json.dumps(data).encode())

    def event(self, row, refetch_within_ms):
        return self._block(row.type, {"id": row.id, "refetch_within_ms": refetch_within_ms, **json.loads(row.payload)})

    def ready(self):
        return self._block("ready", {})

    def heartbeat(self):
        return self._block("heartbeat", {})

    def reload(self, refetch_within_ms):
        return self._block("reload", {"refetch_within_ms": refetch_within_ms})

    def rights_changed(self, refetch_within_ms):
        return self._block("rights_changed", {"refetch_within_ms": refetch_within_ms})

    def close(self, reason):
        return self._block("close", {"reason": reason.value})


def messages(chunk: bytes) -> list:
    """(id, retry, event, data) of every block of a chunk"""
    result = []
    for text in chunk.decode().split("\n\n"):
        if not text:
            continue
        fields = dict(line.split(": ", 1) for line in text.split("\n"))
        result.append((int(fields["id"]), fields.get("retry"), fields["event"], json.loads(fields["data"])))
    return result


def queued(connection) -> list:
    return messages(b"".join(block for block, _ in connection.queue))


@pytest.fixture
def events_on(monkeypatch):
    monkeypatch.setattr(settings.events, "enabled", True)


def config(**overrides) -> EventsConfig:
    return EventsConfig(**{"enabled": True, **overrides})


async def hub_of(engine, **overrides) -> EventHub:
    hub = EventHub(engine, TextRenderer(), config(**overrides), workers=4)
    await hub.start(run_tasks=False)
    return hub


def auth_of(user_id: int, *grants: Grant, session_id: str = "s") -> AuthContext:
    return AuthContext(user=User(id=user_id, username=f"u{user_id}", username_alias="U"), grants=grants,
                       session_id=session_id)


def grant(node: Node, *permissions: P) -> Grant:
    return Grant(1, "role", "Role", False, node, frozenset(p.value for p in permissions), assignment_id=1)


def later() -> float:
    return (datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()


async def _publish(db, event, **kwargs):
    publish(db, event, **kwargs)
    await db.commit()


async def test_a_row_for_a_post_reaches_its_holder_not_a_stranger(engine, db, events_on):
    hub = await hub_of(engine)
    holder = hub.attach(auth_of(1, grant(ROOT, P.POST_UPDATE)), later(), None)
    stranger = hub.attach(auth_of(2, grant(Node.post(99), P.POST_UPDATE)), later(), None)
    await _publish(db, EventType.MOVE_CHANGED, scope=Node.post(7), permission=P.POST_UPDATE, move_id=1,
                   move_status="done")
    assert await hub.tick() == 1
    assert [event for _, _, event, _ in queued(holder)] == ["ready", "move.changed"]
    assert [event for _, _, event, _ in queued(stranger)] == ["ready"]


async def test_user_id_reaches_that_user_with_no_rights_at_all(engine, db, events_on):
    hub = await hub_of(engine)
    requester = hub.attach(auth_of(3), later(), None)
    await _publish(db, EventType.MOVE_CHANGED, scope=Node.post(7), permission=P.POST_UPDATE, user_id=3, move_id=1,
                   move_status="failed")
    await hub.tick()
    assert queued(requester)[-1][2:] == ("move.changed", {"id": 1, "refetch_within_ms": 133, "move_id": 1,
                                                          "move_status": "failed"})


def test_the_refetch_window():
    hub = EventHub(None, TextRenderer(), config(), workers=4)
    assert hub._refetch_window(1000) == 60000
    assert hub._refetch_window(3) == 400
    assert hub._refetch_window(0) == 0


async def test_a_scope_change_in_a_tick_changes_who_gets_the_rows_of_that_tick(engine, db, events_on):
    group, other = await make_group(db), await make_group(db)
    post = await make_post(db, group_id=group.id)
    hub = await hub_of(engine)
    watcher = hub.attach(auth_of(1, grant(Node.group(other.id), P.POST_VIEW, P.POST_UPDATE)), later(), None)
    # the post moves into the watcher's group, and a move event of the post comes in the same tick
    post.group_id = other.id
    db.add(post)
    publish(db, EventType.SCOPE_CHANGED, scope=Node.post(post.id), permission=P.POST_VIEW)
    publish(db, EventType.MOVE_CHANGED, scope=Node.post(post.id), permission=P.POST_UPDATE, move_id=4,
            move_status="done")
    await db.commit()
    await hub.tick()
    assert [event for _, _, event, _ in queued(watcher)] == ["ready", "scope.changed", "move.changed",
                                                             "rights_changed"], \
        "the post now in the watcher's group is in its post_permissions (GET /auth/me)"


async def test_a_slow_connection_never_holds_more_than_queue_max_and_ends(engine, db, events_on):
    hub = await hub_of(engine, queue_max=5, replay_max=4)
    slow = hub.attach(auth_of(1, grant(ROOT, P.POST_VIEW)), later(), None)
    for _ in range(10):
        publish(db, EventType.SCOPE_CHANGED, scope=ROOT, permission=P.POST_VIEW)
    await db.commit()
    await hub.tick()
    assert slow.slow and len(slow.queue) == 0
    chunks = [chunk async for chunk in slow.frames()]
    assert chunks == [] and slow not in hub.connections and hub.slow_closed == 1


async def test_conflation_keeps_the_last_of_a_key(engine, events_on):
    hub = await hub_of(engine)
    connection = hub.attach(auth_of(1), later(), None)
    connection.offer(b"a", "k")
    connection.offer(b"b", "other")
    connection.offer(b"c", "k")
    assert [block for block, _ in connection.queue][-2:] == [b"b", b"c"]


async def test_a_token_past_its_exp_gets_close_token_expired(engine, events_on):
    hub = await hub_of(engine)
    expired = hub.attach(auth_of(1), datetime.now(timezone.utc).timestamp() - 1, None)
    live = hub.attach(auth_of(2), later(), None)
    await hub.beat()
    assert queued(expired)[-1][2:] == ("close", {"reason": "token_expired"})
    assert expired.closing is StreamCloseReason.TOKEN_EXPIRED and live.open
    chunks = [chunk async for chunk in expired.frames()]
    assert len(chunks) == 1 and expired not in hub.connections


async def test_heartbeats_carry_the_position_and_are_spread(engine, db, events_on):
    hub = await hub_of(engine, heartbeat_seconds=20)
    connection = hub.attach(auth_of(1), later(), None)
    await _publish(db, EventType.SCOPE_CHANGED, scope=ROOT, permission=P.AUDIT_VIEW)   # nobody here holds it
    await hub.tick()
    connection.next_beat = 0
    await hub.beat()
    last_id, _, event, _ = queued(connection)[-1]
    assert event == "heartbeat" and last_id == hub.position == hub.gaps.head
    assert connection.next_beat > hub.clock() + 19
    before = len(connection.queue)
    await hub.beat()
    assert len(connection.queue) == before, "not due again"


async def test_drain_closes_everything_with_a_retry_in_range(engine, events_on):
    hub = await hub_of(engine, drain_retry_max_ms=5000)
    connections = [hub.attach(auth_of(user_id), later(), None) for user_id in (1, 2, 3)]
    hub.drain()
    for connection in connections:
        _, retry, event, data = queued(connection)[-1]
        assert (event, data) == ("close", {"reason": "restart"}) and 1000 <= int(retry) <= 5000
    assert hub.attach(auth_of(4), later(), None) is None


async def test_full_answers_no_connection(engine, events_on):
    hub = await hub_of(engine, max_streams_per_worker=2)
    assert hub.attach(auth_of(1), later(), None) and hub.attach(auth_of(2), later(), None)
    assert hub.is_full() and hub.attach(auth_of(3), later(), None) is None


async def test_the_prelude(engine, events_on):
    hub = await hub_of(engine, retry_min_ms=3000, retry_max_ms=3000)
    (ready,) = queued(hub.attach(auth_of(1), later(), None))
    assert ready[1:] == ("3000", "ready", {})


async def test_the_replay_goes_through_attach(engine, db, events_on):
    hub = await hub_of(engine)
    for move_id in (1, 2, 3):
        publish(db, EventType.MOVE_CHANGED, scope=Node.post(7), permission=P.POST_UPDATE, move_id=move_id,
                move_status="done")
    publish(db, EventType.MOVE_CHANGED, scope=Node.post(8), permission=P.POST_UPDATE, move_id=4, move_status="done")
    await db.commit()
    await hub.tick()
    head = hub.gaps.head
    first = head - 3
    replayed = queued(hub.attach(auth_of(1, grant(Node.post(7), P.POST_UPDATE)), later(), str(first)))
    assert [(event, data.get("move_id")) for _, _, event, data in replayed] == [
        ("ready", None), ("move.changed", 2), ("move.changed", 3)], "only what this user may receive"
    assert {message_id for message_id, *_ in replayed} == {hub.position}, "every block carries the current position"


@pytest.mark.parametrize("sent", ["abc", "999999999999", "-1"])
async def test_a_position_we_cannot_answer_for_is_reload(engine, db, events_on, sent):
    hub = await hub_of(engine)
    messages_ = queued(hub.attach(auth_of(1), later(), sent))
    assert [event for _, _, event, _ in messages_] == ["ready", "reload"] and hub.reloads == 1


async def test_a_replay_above_replay_max_is_reload(engine, db, events_on):
    hub = await hub_of(engine, replay_max=2, queue_max=3)
    for _ in range(3):
        publish(db, EventType.SCOPE_CHANGED, scope=ROOT, permission=P.POST_VIEW)
    await db.commit()
    await hub.tick()
    connection = hub.attach(auth_of(1, grant(ROOT, P.POST_VIEW)), later(), str(hub.gaps.head - 3))
    assert [event for _, _, event, _ in queued(connection)] == ["ready", "reload"] and connection.open


async def test_a_position_older_than_the_ring_is_reload(engine, db, events_on):
    hub = await hub_of(engine, ring_rows=2)
    for _ in range(4):
        publish(db, EventType.SCOPE_CHANGED, scope=ROOT, permission=P.POST_VIEW)
    await db.commit()
    await hub.tick()
    connection = hub.attach(auth_of(1, grant(ROOT, P.POST_VIEW)), later(), "1")
    assert [event for _, _, event, _ in queued(connection)] == ["ready", "reload"]


async def test_a_reconnect_after_a_late_commit_gets_the_row(engine, db, events_on):
    """Plan test 6, found on the test server (2026-11-23): an event committed 20 s after its insert, a client that
    reconnects after the commit with a position taken during the hold. The position alone had passed the gap after
    position_hold_seconds, so 49 of 60 reconnected streams never got the event; the cursor names the late id now"""
    from app.backend.models.outbox_event import OutboxEvent
    now = [1000.0]
    hub = EventHub(engine, TextRenderer(), config(position_hold_seconds=8), workers=4, clock=lambda: now[0])
    await hub.start(run_tasks=False)
    holder = auth_of(1, grant(ROOT, P.POST_VIEW))
    publish(db, EventType.SCOPE_CHANGED, scope=ROOT, permission=P.POST_VIEW)
    await db.commit()
    await hub.tick()
    late_id = hub.gaps.head + 1
    db.add(OutboxEvent(id=late_id + 1, type=EventType.SCOPE_CHANGED.value, scope_key="root",
                       permission=P.POST_VIEW.value, payload="{}"))     # a later transaction commits first
    await db.commit()
    await hub.tick()
    now[0] += 9                                                         # the hold is over, the gap still watched
    await hub.tick()
    first = hub.attach(holder, later(), None)
    token = first.queue[0][0].split(b"\n", 1)[0].decode()[len("id: "):]
    assert token == f"{late_id + 1}.1", "the position passed the gap and names it"
    db.add(OutboxEvent(id=late_id, type=EventType.SCOPE_CHANGED.value, scope_key="root",
                       permission=P.POST_VIEW.value, payload="{}"))     # the held transaction commits
    await db.commit()
    await hub.tick()
    assert any(b"\"id\": %d" % late_id in block for block, _ in first.queue), "an open stream gets it live"
    back = hub.attach(auth_of(2, grant(ROOT, P.POST_VIEW)), later(), token)
    replayed = [block for block, _ in back.queue]
    assert any(b"\"id\": %d" % late_id in block for block in replayed), "a reconnect with the old cursor gets it"


async def test_a_position_a_little_ahead_is_no_reload(engine, events_on):
    """A client that read the last second on another worker: nothing to replay, the rest comes live"""
    hub = await hub_of(engine)
    connection = hub.attach(auth_of(1), later(), str(hub.gaps.head + 3))
    assert [event for _, _, event, _ in queued(connection)] == ["ready"]


async def test_the_position_stays_below_a_late_commit(engine, db, events_on):
    from sqlalchemy import insert
    from app.backend.models.outbox_event import OutboxEvent
    hub = await hub_of(engine)
    connection = hub.attach(auth_of(1, grant(ROOT, P.POST_VIEW)), later(), None)
    row = {"type": "scope.changed", "v": 1, "scope_key": "root", "permission": "post.view", "payload": "{}"}
    async with engine.begin() as raw:
        await raw.execute(insert(OutboxEvent.__table__), [{**row, "id": 1}, {**row, "id": 3}])
    await hub.tick()
    assert hub.position == 1 and [message_id for message_id, *_ in queued(connection)][-1] == 1
    async with engine.begin() as raw:
        await raw.execute(insert(OutboxEvent.__table__), [{**row, "id": 2}])
    await hub.tick()
    assert hub.position == 3 and queued(connection)[-1][3]["id"] == 2


async def test_disconnect_detaches_and_drops_the_users_grants(engine, events_on):
    hub = await hub_of(engine)
    connection = hub.attach(auth_of(1, grant(ROOT, P.POST_VIEW)), later(), None)
    frames = connection.frames()
    assert (await frames.__anext__()).startswith(b"id: ")
    await frames.aclose()                     # the client went away: Starlette cancels the body
    assert hub.connections == set() and hub.by_user == {} and hub.rights.grants == {}


async def test_two_tabs_of_one_user_each_receive(engine, db, events_on):
    hub = await hub_of(engine)
    tabs = [hub.attach(auth_of(1, grant(ROOT, P.POST_VIEW)), later(), None) for _ in range(2)]
    await _publish(db, EventType.SCOPE_CHANGED, scope=ROOT, permission=P.POST_VIEW)
    await hub.tick()
    assert all(queued(tab)[-1][2] == "scope.changed" for tab in tabs)
    tabs[0].ended = True
    hub.detach(tabs[0])
    assert 1 in hub.rights.grants, "the other tab keeps the user's grants"


async def test_the_session_check_closes_ended_sessions(engine, db, events_on):
    user = await make_user(db)
    now = datetime.now(timezone.utc)
    for session_id, ended in (("live", None), ("gone", now)):
        db.add(UserSession(id=session_id, user_id=user.id, created_at=now, last_activity=now,
                           expires_at=now + timedelta(minutes=30), ended_at=ended))
    await db.commit()
    hub = await hub_of(engine)
    live = hub.attach(auth_of(user.id, session_id="live"), later(), None)
    gone = hub.attach(auth_of(user.id, session_id="gone"), later(), None)
    await hub.check_sessions()
    assert live.open and queued(gone)[-1][2:] == ("close", {"reason": "session_ended"})


async def test_a_stream_that_attaches_during_the_session_check_is_not_closed(engine, db, events_on, monkeypatch):
    """The check reads the sessions it collected, then awaits the database: a tab that attaches meanwhile was not in
    that query, so it must not count as ended (found on the test server: 2 of 200 browsers got session_ended at once
    after their login, their sessions live)"""
    user = await make_user(db)
    now = datetime.now(timezone.utc)
    for session_id in ("old", "new"):
        db.add(UserSession(id=session_id, user_id=user.id, created_at=now, last_activity=now,
                           expires_at=now + timedelta(minutes=30)))
    await db.commit()
    hub = await hub_of(engine)
    old = hub.attach(auth_of(user.id, session_id="old"), later(), None)
    import app.backend.services.events.hub as hub_module
    real = hub_module.live_sessions
    late = []

    async def slow_query(session, session_ids, **kwargs):
        alive = await real(session, session_ids, **kwargs)
        late.append(hub.attach(auth_of(user.id, session_id="new"), later(), None))   # a login in the meantime
        return alive

    monkeypatch.setattr(hub_module, "live_sessions", slow_query)
    await hub.check_sessions()
    assert old.open and late[0].open


async def test_the_rights_refresh_follows_a_lost_grant(engine, db, events_on):
    user = await make_user(db)
    roles = await give(db, user, [P.POST_VIEW])
    auth = await load_auth_context(db, user, session_id="s")
    hub = await hub_of(engine)
    connection = hub.attach(auth, later(), None)
    assert list(hub.rights.holders("p:7", P.POST_VIEW.value)) == [user.id]
    from sqlmodel import delete
    from app.backend.models.role_assignment import RoleAssignment
    await db.exec(delete(RoleAssignment).where(RoleAssignment.user_id == user.id))
    await db.commit()
    await hub.refresh_rights()
    assert list(hub.rights.holders("p:7", P.POST_VIEW.value)) == [] and connection.open
    assert roles


async def test_a_loop_survives_an_error(engine, events_on, monkeypatch):
    hub = await hub_of(engine)
    calls = []

    async def failing():
        calls.append(1)
        raise RuntimeError("boom")

    task = asyncio.create_task(hub._loop(failing, 0.001))
    while len(calls) < 3:
        await asyncio.sleep(0.005)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_the_rows_read_at_start_do_not_count_as_lag(engine, db, events_on):
    from sqlalchemy import insert
    from app.backend.models.outbox_event import OutboxEvent
    old = (datetime.now(timezone.utc) - timedelta(seconds=30)).replace(tzinfo=None)
    async with engine.begin() as raw:
        await raw.execute(insert(OutboxEvent.__table__), [{"type": "scope.changed", "v": 1, "scope_key": "root",
                                                           "permission": "post.view", "payload": "{}",
                                                           "occurred_at": old}])
    hub = await hub_of(engine)
    assert len(hub.ring) == 1, "the start fills the ring"
    assert hub.stats()["lag_ms"] == 0, "filling the ring is no delivery"


async def test_a_failed_tree_reload_loses_no_row_and_moves_no_position(engine, db, events_on, monkeypatch):
    """Review: the head and the position moved before the await of the tree; a failure there dropped the rows"""
    hub = await hub_of(engine)
    connection = hub.attach(auth_of(1, grant(ROOT, P.POST_VIEW)), later(), None)
    start = hub.position
    await _publish(db, EventType.SCOPE_CHANGED, scope=ROOT, permission=P.POST_VIEW)
    original = hub.reload_tree

    async def broken():
        raise RuntimeError("database blip")

    monkeypatch.setattr(hub, "reload_tree", broken)
    with pytest.raises(RuntimeError):
        await hub.tick()
    assert hub.position == start and hub.gaps.head == start, "nothing counted as read"
    monkeypatch.setattr(hub, "reload_tree", original)
    await hub.tick()
    assert queued(connection)[-1][2] == "scope.changed" and hub.position == start + 1


async def test_one_row_that_fails_to_render_does_not_drop_the_others(engine, db, events_on):
    hub = await hub_of(engine)
    connection = hub.attach(auth_of(1, grant(ROOT, P.POST_VIEW, P.POST_UPDATE)), later(), None)
    publish(db, EventType.MOVE_CHANGED, scope=ROOT, permission=P.POST_UPDATE, move_id=1, move_status="done")
    publish(db, EventType.MOVE_CHANGED, scope=ROOT, permission=P.POST_UPDATE, move_id=2, move_status="done")
    await db.commit()
    render = hub.render.event

    def picky(row, refetch_within_ms):
        if json.loads(row.payload)["move_id"] == 1:
            raise ValueError("a row this renderer cannot write")
        return render(row, refetch_within_ms)

    hub.render.event = picky
    assert await hub.tick() == 2
    assert [data.get("move_id") for _, _, event, data in queued(connection) if event == "move.changed"] == [2]


async def test_an_archive_reaches_the_holders_who_lose_the_node(engine, db, events_on):
    """Review: delivery after the tree reload left out the archived node, so its own holders never heard of it"""
    group = await make_group(db)
    post = await make_post(db, group_id=group.id)
    hub = await hub_of(engine)
    at_post = hub.attach(auth_of(1, grant(Node.post(post.id), P.POST_VIEW)), later(), None)
    at_group = hub.attach(auth_of(2, grant(Node.group(group.id), P.POST_VIEW)), later(), None)
    stranger = hub.attach(auth_of(3, grant(Node.post(post.id + 100), P.POST_VIEW)), later(), None)
    post.is_deleted = True
    db.add(post)
    publish(db, EventType.SCOPE_CHANGED, scope=Node.post(post.id), permission=P.POST_VIEW)
    await db.commit()
    await hub.tick()
    assert [event for _, _, event, _ in queued(at_post)] == ["ready", "scope.changed", "rights_changed"], \
        "the holder at the archived post is told, and its grant there no longer applies"
    assert [event for _, _, event, _ in queued(at_group)] == ["ready", "scope.changed"], \
        "a group's grant keeps applying to its archived posts"
    assert [event for _, _, event, _ in queued(stranger)] == ["ready"]


# ---------------------------------------------------------------- rights_changed

def events_of(connection) -> list:
    return [event for _, _, event, _ in queued(connection)]


async def test_no_rights_changed_at_connect(engine, events_on):
    """The grants of a connect are the baseline; a second tab with the same rights tells the first nothing"""
    hub = await hub_of(engine)
    first = hub.attach(auth_of(1, grant(ROOT, P.POST_VIEW)), later(), None)
    second = hub.attach(auth_of(1, grant(ROOT, P.POST_VIEW)), later(), None)
    other = hub.attach(auth_of(2), later(), None)
    assert [events_of(c) for c in (first, second, other)] == [["ready"]] * 3


async def test_a_new_tab_with_other_rights_tells_the_tabs_already_open(engine, events_on):
    """The request of a new tab read the user's grants after the change: the open tabs refetch, the new one read
    GET /auth/me itself"""
    hub = await hub_of(engine)
    first = hub.attach(auth_of(1, grant(ROOT, P.POST_VIEW)), later(), None)
    second = hub.attach(auth_of(1, grant(ROOT, P.POST_VIEW, P.POST_UPDATE)), later(), None)
    assert events_of(first) == ["ready", "rights_changed"] and events_of(second) == ["ready"]


async def test_the_rights_refresh_finding_an_archived_node_tells_its_users(engine, db, events_on):
    """No event names the user (an archive made with events off, a type boundary, anything): the 30 s refresh compares
    what is in effect and tells every tab of a user whose rights changed, nobody else"""
    post, elsewhere = await make_post(db), await make_post(db)
    holder = await make_user(db, user_type=UserType.VIEWER)
    await give(db, holder, [P.POST_VIEW, P.OBSERVATION_VIEW], Node.post(post.id))
    other = await make_user(db, user_type=UserType.VIEWER)
    await give(db, other, [P.POST_VIEW], Node.post(elsewhere.id))
    hub = await hub_of(engine)
    tabs = [hub.attach(await load_auth_context(db, holder, session_id=f"s{n}"), later(), None) for n in range(2)]
    stranger = hub.attach(await load_auth_context(db, other, session_id="o"), later(), None)
    await hub.refresh_rights()
    assert [events_of(c) for c in (*tabs, stranger)] == [["ready"]] * 3, "nothing changed, nothing sent"

    post.is_deleted = True
    db.add(post)
    await db.commit()
    await hub.refresh_rights()
    for tab in tabs:
        assert queued(tab)[-1][2:] == ("rights_changed", {"refetch_within_ms": hub._refetch_window(2)})
    assert events_of(stranger) == ["ready"]
    for tab in tabs:
        tab.queue.clear()
    await hub.refresh_rights()
    assert [events_of(tab) for tab in tabs] == [[], []], "told once per change"


async def test_rights_changed_waits_once_in_a_queue(engine, db, events_on):
    """Conflated: a stream that has not read the last one keeps one, the latest, behind what came after it"""
    post = await make_post(db)
    holder = await make_user(db, user_type=UserType.VIEWER)
    await give(db, holder, [P.POST_VIEW], Node.post(post.id))
    hub = await hub_of(engine)
    tab = hub.attach(await load_auth_context(db, holder, session_id="s"), later(), None)
    post.is_deleted = True
    db.add(post)
    await db.commit()
    await hub.refresh_rights()
    await _publish(db, EventType.MOVE_CHANGED, user_id=holder.id, move_id=1, move_status="done")
    await hub.tick()
    assert events_of(tab) == ["ready", "rights_changed", "move.changed"]
    post.is_deleted = False
    db.add(post)
    await db.commit()
    await hub.refresh_rights()
    assert events_of(tab) == ["ready", "move.changed", "rights_changed"]


async def test_a_user_who_left_has_no_fingerprint_left(engine, events_on):
    hub = await hub_of(engine)
    connection = hub.attach(auth_of(1, grant(ROOT, P.POST_VIEW)), later(), None)
    connection.ended = True
    hub.detach(connection)
    assert hub._fingerprints == {}


async def _holder_at_new_post(db):
    post = await make_post(db)
    holder = await make_user(db, user_type=UserType.VIEWER)
    await give(db, holder, [P.POST_VIEW], Node.post(post.id))
    return post, holder


def _leave(hub, connection) -> str:
    """The stream drops; the client keeps its position for the reconnect"""
    position = str(hub.position)
    connection.ended = True
    hub.detach(connection)
    return position


async def _archive(db, post, archived=True):
    post.is_deleted = archived
    db.add(post)
    await db.commit()


async def test_a_tab_that_reconnects_after_a_change_it_missed_is_told(engine, db, events_on):
    """The only stream of a user drops; its rights change meanwhile (nobody here to tell); the reconnect with its
    position is told, the baseline of a first connect does not swallow it"""
    post, holder = await _holder_at_new_post(db)
    hub = await hub_of(engine)
    position = _leave(hub, hub.attach(await load_auth_context(db, holder, session_id="s"), later(), None))
    await _archive(db, post)
    await hub.refresh_rights()
    back = hub.attach(await load_auth_context(db, holder, session_id="s"), later(), position)
    assert events_of(back) == ["ready", "rights_changed"]


async def test_a_reconnect_with_nothing_changed_and_a_fresh_connect_are_not_told(engine, db, events_on):
    post, holder = await _holder_at_new_post(db)
    hub = await hub_of(engine)
    position = _leave(hub, hub.attach(await load_auth_context(db, holder, session_id="s"), later(), None))
    await hub.refresh_rights()
    back = hub.attach(await load_auth_context(db, holder, session_id="s"), later(), position)
    assert events_of(back) == ["ready"]
    _leave(hub, back)
    await _archive(db, post)
    await hub.refresh_rights()
    fresh = hub.attach(await load_auth_context(db, holder, session_id="s"), later(), None)
    assert events_of(fresh) == ["ready"], "a page load reads GET /auth/me itself"


async def test_a_tab_that_missed_the_tell_to_its_other_tabs_is_told_on_reconnect(engine, db, events_on):
    post, holder = await _holder_at_new_post(db)
    hub = await hub_of(engine)
    staying = hub.attach(await load_auth_context(db, holder, session_id="s"), later(), None)
    position = _leave(hub, hub.attach(await load_auth_context(db, holder, session_id="s"), later(), None))
    await _archive(db, post)
    await hub.refresh_rights()
    assert events_of(staying) == ["ready", "rights_changed"]
    back = hub.attach(await load_auth_context(db, holder, session_id="s"), later(), position)
    assert events_of(back) == ["ready", "rights_changed"]
    assert events_of(staying) == ["ready", "rights_changed"], "the tab already told is not told again"


async def test_what_a_departed_user_leaves_is_dropped_once_the_ring_moved_past_it(engine, db, events_on):
    """A position below the ring gets reload: the hub forgets a departed user's rights once the ring floor passed the
    moment they left"""
    post, holder = await _holder_at_new_post(db)
    hub = await hub_of(engine, ring_rows=1)
    _leave(hub, hub.attach(await load_auth_context(db, holder, session_id="s"), later(), None))
    assert holder.id in hub._departed
    for _ in range(3):
        await _publish(db, EventType.SCOPE_CHANGED, scope=ROOT, permission=P.POST_VIEW)
        await hub.tick()
    await hub.refresh_rights()
    assert hub._departed == {} and hub._told_at == {}


# ---------------------------------------------------------------- rows of a newer release (rollback by tag)

async def test_old_hub_forwards_a_row_of_an_unknown_type(engine, db, events_on):
    """After a rollback the previous release reads rows the newer one wrote (RELEASE.md): a type it does not know is
    passed on as it is, a permission it does not know is held by nobody, so that row reaches only its user_id"""
    from app.backend.api.v1.models.response.events import render_v1
    from app.backend.models.outbox_event import OutboxEvent
    hub = await hub_of(engine)
    holder = hub.attach(auth_of(1, grant(ROOT, P.POST_VIEW)), later(), None)
    other = hub.attach(auth_of(2, grant(ROOT, P.POST_VIEW)), later(), None)
    db.add(OutboxEvent(type="future.changed", v=1, scope_key="root", permission=P.POST_VIEW.value,
                       payload=json.dumps({"future_id": 5})))
    db.add(OutboxEvent(type="future.personal", v=1, scope_key="root", permission="future.view", user_id=2,
                       payload=json.dumps({"future_id": 6})))
    await db.commit()
    assert await hub.tick() == 2
    assert [(event, data.get("future_id")) for _, _, event, data in queued(holder)] == [
        ("ready", None), ("future.changed", 5)]
    assert [(event, data.get("future_id")) for _, _, event, data in queued(other)] == [
        ("ready", None), ("future.changed", 5), ("future.personal", 6)]
    row = OutboxEvent(id=1, type="future.changed", v=1, scope_key="root", permission=P.POST_VIEW.value,
                      occurred_at=datetime(2026, 11, 26, 12, 0, 0),
                      payload=json.dumps({"future_id": 5}))
    assert b"event: future.changed" in render_v1.event(row, 0), "the v1 renderer copies an unknown type"
