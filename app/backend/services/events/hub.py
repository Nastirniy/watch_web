"""The hub of one api worker: every stream this process holds.

    pump        every events.poll_seconds (10% jitter): read the outbox (new rows and open gaps), reload the tree when a
                scope.changed came and the grants of the connected users a user.changed or role.changed concerns,
                deliver each row once to the connections whose user may receive it, then rights_changed
    ticker      every second, one task for every connection: heartbeats due, tokens expired
    sessions    every events.session_check_seconds: one batched query, ended sessions close their streams
    rights      every events.rights_refresh_seconds (jitter): the tree and the grants of the connected users, then
                rights_changed
    report      every 15 s: counts into worker_heartbeats (kind events), which /health sums

The hub has its own engine (pool 1, overflow 1): the pump never waits for the request pool, and a stream holds no
connection at all. A block is built once per event and shared by every connection; each connection writes one chunk
per tick. Streams do not spread evenly over workers (one usually holds nearly all): nothing here assumes they do.

rights_changed: the hub keeps, per connected user, the fingerprint of what their grants put in effect on its tree
(RightsIndex.fingerprint, equal means equal rights in GET /auth/me). The grants of the first connect are the baseline;
after every load of the tree or of grants (the pump, the refresh, a new tab whose request read other grants) each user
whose fingerprint moved gets one rights_changed on every open stream (conflated: one waits per stream); a tab that
reconnects with its position is told what it missed meanwhile (its user's last fingerprint and the position of the last
rights_changed stay after the last tab leaves, until the ring passes them: an older position gets reload). So a tab
refetches its own rights though no event names it (a role's permissions for a holder without role.view, an archived
node, a post moved into a group they hold a role at). Nothing is published for it: every hub holding streams of the
user computes the same. It may come with a user.changed or scope.changed of the same tick that asks for GET /auth/me
too.

Rendering belongs to the version (api/v1/models/response/events.py): app/main.py hands the renderer in, because
services never import api. Every loop catches Exception, logs it and goes on (the allowed broad catch of a worker loop).
"""
import asyncio
import json
import os
import random
import signal
import socket
import time
from collections import deque
from datetime import datetime, timezone
from typing import Callable, Deque, Dict, Hashable, Iterable, List, Optional, Protocol, Set, Tuple

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.auth.context import AuthContext, load_grants_of, load_scope_tree
from app.backend.config import EventsConfig
from app.backend.enums.events import EventType, StreamCloseReason
from app.backend.models.user import User
from app.backend.services.events.connection import Connection
from app.backend.services.events.gaps import GapTracker
from app.backend.services.events.reader import OutboxRow, first_head, new_rows_of, read_tick
from app.backend.services.events.rights import RightsIndex
from app.backend.services.events.ring import Ring, block, parse_position
from app.backend.services.events.session_check import live_sessions
from app.backend.services.media import jobs

REPORT_SECONDS = 15
HEARTBEAT_KIND = "events"
READ_BATCH = 1000
# Rows whose receivers are read on the tree or the grants before the change as well (those who just lost the right)
RIGHTS_CHANGES = frozenset({EventType.SCOPE_CHANGED.value, EventType.ROLE_CHANGED.value})
RIGHTS_CHANGED_KEY = "stream:rights_changed"     # the conflate key of rights_changed: one waits per stream


class EveryUser:
    """grants_to_reload: every connected user"""


EVERY_USER = EveryUser()


def grants_to_reload(rows: Iterable[OutboxRow]):
    """Whose grants a tick reloads before it delivers: EVERY_USER after a role.changed (the holders of a role are not
    known for a restored one: an archived role has no grant in the index; roles change rarely, one batch is cheap),
    else the subject of each user.changed (an assignment, a type change, an archive), else nobody (an empty set)"""
    users: Set[int] = set()
    for row in rows:
        if row.type == EventType.ROLE_CHANGED.value:
            return EVERY_USER
        if row.type == EventType.USER_CHANGED.value:
            try:
                users.add(int(json.loads(row.payload)["subject_user_id"]))
            except (ValueError, KeyError, TypeError):     # never written by publish(); a bad row must not stop the pump
                logger.warning(f"Event hub: row {row.id} (user.changed) names no user")
    return users


class Renderer(Protocol):
    """The messages of one API version, as the bytes from "event:" to the empty line"""

    def event(self, row: OutboxRow, refetch_within_ms: int) -> bytes: ...

    def ready(self) -> bytes: ...

    def heartbeat(self) -> bytes: ...

    def reload(self, refetch_within_ms: int) -> bytes: ...

    def rights_changed(self, refetch_within_ms: int) -> bytes: ...

    def close(self, reason: StreamCloseReason) -> bytes: ...


def hub_name() -> str:
    """The row of this process in worker_heartbeats: the hub's report, or the starter's while the hub is down"""
    return f"{HEARTBEAT_KIND}@{socket.gethostname()}:{os.getpid()}"[:64]


def retry_line(milliseconds: int) -> bytes:
    """WHATWG SSE: digits only; fetch does not apply it by itself, the web kit does"""
    return b"retry: %d\n" % milliseconds


class EventHub:
    def __init__(self, engine: AsyncEngine, render: Renderer, config: EventsConfig, workers: int,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time):
        self.engine = engine
        self.render = render
        self.config = config
        self.workers = max(1, workers)
        self.clock, self.wall = clock, wall
        self.sessions = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        self.gaps = GapTracker(ttl=config.gap_ttl_seconds, hold=config.position_hold_seconds,
                               max_jump=config.max_gap_jump, max_gaps=config.max_open_gaps)
        self.ring = Ring(config.ring_rows)
        self.rights = RightsIndex()
        # Loads of the tree and the grants run in two tasks (the pump, the rights refresh) and may overlap: each takes
        # a number when it starts, and an older load never replaces what a newer one set
        self._loads = 0
        self._tree_load = 0
        self._grant_loads: Dict[int, int] = {}
        # user -> the fingerprint of their rights the tabs were last told of (the baseline: the first connect); when the
        # last tab of a user leaves, that fingerprint and the position (departed), and the position of the last
        # rights_changed of each user (told_at), for a tab that reconnects with its position
        self._fingerprints: Dict[int, Hashable] = {}
        self._departed: Dict[int, Tuple[Hashable, int]] = {}
        self._told_at: Dict[int, int] = {}
        self.position = 0
        self.connections: Set[Connection] = set()
        self.by_user: Dict[int, Set[Connection]] = {}
        self.stopping = False
        self.started = False
        self.tasks: List[asyncio.Task] = []
        # counts for /health (since this process started)
        self.slow_closed = 0
        self.reloads = 0
        self._lags: Deque[int] = deque(maxlen=500)
        self.name = hub_name()
        self.started_at = datetime.now(timezone.utc)

    # ------------------------------------------------------------------ lifecycle
    async def start(self, run_tasks: bool = True) -> None:
        """The head of the last gap_ttl_seconds, the tree, one tick without delivery (it fills the ring), the tasks"""
        head = await first_head(self.engine, self.config.gap_ttl_seconds)
        self.gaps.head = head
        self.ring = Ring(self.config.ring_rows, floor=head)
        self.position = head
        await self.reload_tree()
        await self.tick()
        self._lags.clear()                  # filling the ring is no delivery: its rows are old by design
        self.started = True
        if run_tasks:
            await self.report()             # /health sees this worker at once, not after the first interval
            self.tasks = [asyncio.create_task(loop, name=f"events-{name}") for name, loop in (
                ("pump", self._loop(self.tick, self.config.poll_seconds, jitter=0.1)),
                ("ticker", self._loop(self.beat, 1.0)),
                ("sessions", self._loop(self.check_sessions, self.config.session_check_seconds)),
                ("rights", self._loop(self.refresh_rights, self.config.rights_refresh_seconds, jitter=0.1)),
                ("report", self._loop(self.report, REPORT_SECONDS)),
            )]
        logger.info(f"Event hub started at position {head}")

    async def stop(self) -> None:
        """Cancel the tasks, remove the report row, dispose the engine"""
        self.drain()
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        if self.tasks:
            async with self.sessions() as session:
                await jobs.remove_heartbeat(session, self.name)
        self.tasks = []
        await self.engine.dispose()

    async def _loop(self, work: Callable, seconds: float, jitter: float = 0.0) -> None:
        while True:
            await asyncio.sleep(seconds * (1 + random.uniform(-jitter, jitter)) if jitter else seconds)
            try:
                await work()
            except asyncio.CancelledError:
                raise
            except Exception:       # a worker loop: log and go on, the next round tries again
                logger.exception(f"Event hub: {getattr(work, '__name__', work)} failed")

    def drain(self) -> None:
        """The stop: every stream gets close restart with a random retry, new connects are refused. uvicorn runs
        lifespan shutdown only after every connection ended, so this runs from the SIGTERM handler"""
        if self.stopping:
            return
        self.stopping = True
        for connection in list(self.connections):
            retry = random.randint(1000, max(1000, self.config.drain_retry_max_ms))
            self._close(connection, StreamCloseReason.RESTART, retry)
        logger.info(f"Event hub draining: {len(self.connections)} stream(s) told to reconnect")

    def install_drain(self, loop: asyncio.AbstractEventLoop) -> None:
        """uvicorn runs lifespan shutdown only after every connection ended, and streams never end: the stop must reach
        them first. Its own handler goes on running: it is called after ours. Tested on asyncio and uvloop."""
        previous = signal.getsignal(signal.SIGTERM)

        def on_term(signum, frame):
            loop.call_soon_threadsafe(self.drain)
            if callable(previous):
                previous(signum, frame)

        try:
            signal.signal(signal.SIGTERM, on_term)
        except ValueError:          # not the main thread (tests, an embedded server): no drain by signal
            logger.info("Event hub: SIGTERM handler not installed (not the main thread)")

    # ------------------------------------------------------------------ the pump
    def _next_load(self) -> int:
        self._loads += 1
        return self._loads

    def _set_tree(self, tree, load: int) -> None:
        if load > self._tree_load:
            self._tree_load = load
            self.rights.set_tree(tree)

    async def reload_tree(self) -> None:
        load = self._next_load()
        async with self.sessions() as session:
            tree = await load_scope_tree(session)
            await session.rollback()
        self._set_tree(tree, load)

    async def tick(self) -> int:
        """One round of the pump: every row committed since the last one, delivered. The number of rows.

        Every await comes first (the read, the tree, the grants): a failure there leaves the head and the position
        where they were, so the next tick reads the same rows again, and no heartbeat can carry a position past rows
        not yet delivered. What follows has no await: the rows count as read, enter the ring and are delivered at
        once."""
        total = 0
        while True:
            head = self.gaps.head
            rows = await read_tick(self.engine, self.gaps, READ_BATCH)
            # scope.changed and role.changed reach the holders on the tree and the grants before the change too:
            # those who just lost the node (an archive, a move away) or the right are the ones whose view changed.
            # The tree and the grants after it are loaded before any delivery
            before: Dict[int, Set[Connection]] = {row.id: set(self._targets(row)) for row in rows
                                                  if row.type in RIGHTS_CHANGES}
            tree_changed = any(row.type == EventType.SCOPE_CHANGED.value for row in rows)
            if tree_changed:
                await self.reload_tree()
            reload = grants_to_reload(rows)
            if reload:
                await self.reload_grants(None if reload is EVERY_USER else reload)
            now = self.clock()
            self.gaps.saw((row.id for row in rows), now)
            self.gaps.expire(now)
            self.position = self.gaps.position(now)
            for row in rows:
                self._deliver(row, before.get(row.id, set()))
            if tree_changed or reload is EVERY_USER:
                self._tell_rights_changed(list(self.by_user))
            elif reload:
                self._tell_rights_changed(reload)
            total += len(rows)
            if new_rows_of(rows, head) < READ_BATCH:
                return total

    def _deliver(self, row: OutboxRow, also: Set[Connection]) -> None:
        """One row to the ring and its connections; a row that cannot be rendered is logged and skipped, never the
        others (the allowed broad catch: the pump goes on)"""
        try:
            self._lags.append(row.lag_ms)
            targets = set(self._targets(row)) | {connection for connection in also if connection.open}
            body = self.render.event(row, self._refetch_window(len(targets)))
            self.ring.add(row, body)
            frame = block(self.position, body)      # built once, shared by every connection
            for connection in targets:
                connection.offer(frame, row.conflate_key)
        except Exception:
            logger.exception(f"Event hub: row {row.id} ({row.type}) not delivered")

    def _refetch_window(self, subscribers: int) -> int:
        """One event never asks the API for more than refetch_rate_per_second: each worker spreads its own clients, so
        the sum holds whatever the skew (measured: 1,000 refetches at once, p95 5.6 s; over 30 s, p95 126 ms)"""
        window = subscribers * self.workers * 1000 // self.config.refetch_rate_per_second
        return min(window, self.config.refetch_window_max_ms)

    def _may_receive(self, user_id: int, row: OutboxRow) -> bool:
        if row.user_id == user_id:
            return True
        return bool(row.scope_key and self.rights.allows(user_id, row.scope_key, row.permission))

    def _targets(self, row: OutboxRow) -> List[Connection]:
        """Connections of the holders of the permission at the scope, plus those of row.user_id, each once"""
        users: Set[int] = set()
        if row.scope_key:
            users.update(self.rights.holders(row.scope_key, row.permission))
        if row.user_id is not None:
            users.add(row.user_id)
        return [connection for user_id in users for connection in self.by_user.get(user_id, ())
                if connection.open]

    # ------------------------------------------------------------------ connections
    def is_full(self) -> bool:
        return len(self.connections) >= self.config.max_streams_per_worker

    def attach(self, auth: AuthContext, expires_at: float, last_event_id: Optional[str]) -> Optional[Connection]:
        """A new stream: the grants from the request's AuthContext (a connect costs no extra query), the prelude
        (retry, ready, the replay or reload). None when this worker is stopping or full."""
        if self.stopping or self.is_full():
            return None
        user_id = auth.user.id
        now = self.clock()
        connection = Connection(user_id, auth.session_id, expires_at, self.config.queue_max,
                                next_beat=now + random.uniform(0, self.config.heartbeat_seconds), opened_at=now)
        connection.on_end = self.detach
        connected = user_id in self.by_user
        known = self._fingerprints.get(user_id) if connected else self._departed.pop(user_id, (None, 0))[0]
        told_at = self._told_at.get(user_id)
        self.rights.set_user(user_id, auth.grants)
        self._grant_loads[user_id] = self._next_load()
        fingerprint = self.rights.fingerprint(user_id)
        if connected:
            self._tell_rights_changed([user_id])    # the tabs already open, before this one joins them
        else:
            self._fingerprints[user_id] = fingerprint
        retry = random.randint(self.config.retry_min_ms, self.config.retry_max_ms)
        connection.offer(block(self.position, retry_line(retry) + self.render.ready()))
        replay = self._replay(user_id, last_event_id)
        if replay is None:
            self.reloads += 1
            connection.offer(block(self.position, self.render.reload(self._refetch_window(len(self.connections) + 1))))
        else:
            for body in replay:
                connection.offer(block(self.position, body))
            # A tab back with its position: told when its rights moved while it was away (since the user's last tab
            # here left, or since the open tabs were last told) or when the other tabs were told after its position
            if last_event_id is not None and known is not None and (
                    known != fingerprint or (told_at is not None and told_at >= parse_position(last_event_id))):
                self._told_at[user_id] = self.position
                connection.offer(block(self.position, self.render.rights_changed(self._refetch_window(1))),
                                 RIGHTS_CHANGED_KEY)
        self.connections.add(connection)
        self.by_user.setdefault(user_id, set()).add(connection)
        return connection

    def _replay(self, user_id: int, last_event_id: Optional[str]) -> Optional[List[bytes]]:
        """The bodies to send after the position the client sent; [] for none; None for reload. A position a little
        ahead of this worker's head comes from a worker that read the last second first: what lies between is seen
        by that client already, and what comes after reaches it live (at least once)"""
        if last_event_id is None:
            return []
        position = parse_position(last_event_id)
        if position is None:
            return None
        head = self.gaps.head
        if head < position <= head + self.config.max_gap_jump:
            return []
        frames = self.ring.after(position, head, self.config.replay_max,
                                 match=lambda row: self._may_receive(user_id, row))
        return None if frames is None else [body for _, body in frames]

    def detach(self, connection: Connection) -> None:
        if connection not in self.connections:
            return
        self.connections.discard(connection)
        others = self.by_user.get(connection.user_id)
        if others is not None:
            others.discard(connection)
            if not others:
                del self.by_user[connection.user_id]
                self.rights.drop_user(connection.user_id)
                self._grant_loads.pop(connection.user_id, None)
                # what its tabs were told, for a tab that comes back with its position (dropped once the ring passed)
                self._departed[connection.user_id] = (self._fingerprints.pop(connection.user_id, None), self.position)
        if connection.slow:
            self.slow_closed += 1
        reason = "slow" if connection.slow else (connection.closing.value if connection.closing else "client")
        logger.info(f"Event stream closed: user_id={connection.user_id}, session_id={connection.session_id}, "
                    f"reason={reason}, seconds={self.clock() - connection.opened_at:.0f}")

    def _close(self, connection: Connection, reason: StreamCloseReason, retry_ms: Optional[int] = None) -> None:
        body = self.render.close(reason)
        if retry_ms is not None:
            body = retry_line(retry_ms) + body
        connection.close(reason, block(self.position, body), retry_ms)

    # ------------------------------------------------------------------ periodic work
    async def beat(self) -> None:
        """Heartbeats due and tokens expired, for every connection in one pass (no timer per stream). A connection whose
        response never started reading is dropped once it has been closed for a minute."""
        now, wall = self.clock(), self.wall()
        heartbeat: Optional[bytes] = None
        for connection in list(self.connections):
            if not connection.open:
                if connection.closed_at is not None and now - connection.closed_at > 60:
                    self.detach(connection)
                continue
            if wall >= connection.expires_at:
                self._close(connection, StreamCloseReason.TOKEN_EXPIRED)
            elif now >= connection.next_beat:
                if heartbeat is None:
                    heartbeat = block(self.position, self.render.heartbeat())
                connection.offer(heartbeat)
                connection.next_beat = now + self.config.heartbeat_seconds

    async def check_sessions(self) -> None:
        """Streams of sessions that ended (logout, idle, revocation, an account or certificate change) close"""
        session_ids = {connection.session_id for connection in self.connections if connection.open}
        session_ids.discard(None)
        if not session_ids:
            return
        async with self.sessions() as session:
            alive = await live_sessions(session, sorted(session_ids))
            await session.rollback()
        # Only the sessions the query asked about: a tab that attached during the await was not in it (closing it as
        # ended killed fresh streams right after their login, measured on the test server)
        for connection in list(self.connections):
            if connection.open and connection.session_id in session_ids and connection.session_id not in alive:
                self._close(connection, StreamCloseReason.SESSION_ENDED)

    async def refresh_rights(self) -> None:
        """The tree, then the grants of the connected users, in one batch on the hub's engine"""
        user_ids = sorted(self.by_user)
        load = self._next_load()
        async with self.sessions() as session:
            tree = await load_scope_tree(session)
            grants = await self._load_grants(session, user_ids)
            await session.rollback()
        self._set_tree(tree, load)
        self._set_grants(user_ids, grants, load)
        self._tell_rights_changed(list(self.by_user))
        self._forget_rights_before(self.ring.floor)

    async def reload_grants(self, user_ids: Optional[Iterable[int]] = None) -> None:
        """The grants of these connected users (None: every one) at once, in one batch on the hub's engine: a
        user.changed (assignments, a type change) or a role.changed (its permissions, archive, restore) moves the
        rights of a stream before its events are delivered, not at the next rights refresh"""
        connected = set(self.by_user)
        wanted = sorted(connected if user_ids is None else connected & set(user_ids))
        if not wanted:
            return
        load = self._next_load()
        async with self.sessions() as session:
            grants = await self._load_grants(session, wanted)
            await session.rollback()
        self._set_grants(wanted, grants, load)

    @staticmethod
    async def _load_grants(session: AsyncSession, user_ids: List[int]) -> Dict[int, tuple]:
        users = list((await session.exec(select(User).where(User.id.in_(user_ids)))).all()) if user_ids else []
        return await load_grants_of(session, users)

    def _set_grants(self, user_ids: List[int], grants: Dict[int, tuple], load: int) -> None:
        for user_id in user_ids:
            # still connected after the awaits, and no newer load (a connect, the pump) set them meanwhile
            if user_id in self.by_user and load > self._grant_loads.get(user_id, 0):
                self._grant_loads[user_id] = load
                self.rights.set_user(user_id, grants.get(user_id, ()))

    def _tell_rights_changed(self, user_ids: Iterable[int]) -> None:
        """rights_changed to every open stream of the connected users whose fingerprint moved since they were last told
        (no await: called after the last await of a tick or a refresh). One body for all, its refetch window spread
        over every stream told"""
        told: List[Connection] = []
        for user_id in user_ids:
            connections = self.by_user.get(user_id)
            if not connections:
                continue
            fingerprint = self.rights.fingerprint(user_id)
            if fingerprint == self._fingerprints.get(user_id):
                continue
            self._fingerprints[user_id] = fingerprint
            self._told_at[user_id] = self.position
            told.extend(connection for connection in connections if connection.open)
        if told:
            frame = block(self.position, self.render.rights_changed(self._refetch_window(len(told))))
            for connection in told:
                connection.offer(frame, RIGHTS_CHANGED_KEY)

    def _forget_rights_before(self, floor: int) -> None:
        """A position below the ring floor gets reload, never a replay: what a reconnect from before it would compare
        with is dropped"""
        self._departed = {user_id: left for user_id, left in self._departed.items() if left[1] >= floor}
        self._told_at = {user_id: told for user_id, told in self._told_at.items() if told >= floor}

    def stats(self) -> Dict[str, int]:
        lags = sorted(self._lags)
        lag_p95 = lags[min(len(lags) - 1, int(len(lags) * 0.95))] if lags else 0
        return {"streams": len(self.connections), "lag_ms": lag_p95, "gaps": len(self.gaps.open_ids()),
                "slow_closed": self.slow_closed, "reloads": self.reloads}

    async def report(self) -> None:
        """Counts into worker_heartbeats (kind events): /health sums the rows, a request sees one worker only"""
        async with self.sessions() as session:
            await jobs.heartbeat(session, name=self.name, kind=HEARTBEAT_KIND, hostname=socket.gethostname()[:128],
                                 pid=os.getpid(), started_at=self.started_at, jobs_done=0, jobs_failed=0,
                                 details=json.dumps(self.stats(), separators=(",", ":")))
