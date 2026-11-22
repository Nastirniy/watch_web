"""The hub of one api worker: every stream this process holds.

    pump        every events.poll_seconds (10% jitter): read the outbox (new rows and open gaps), reload the tree when a
                scope.changed came, deliver each row once to the connections whose user may receive it
    ticker      every second, one task for every connection: heartbeats due, tokens expired
    sessions    every events.session_check_seconds: one batched query, ended sessions close their streams
    rights      every events.rights_refresh_seconds (jitter): the tree and the grants of the connected users
    report      every 15 s: counts into worker_heartbeats (kind events), which /health sums

The hub has its own engine (pool 1, overflow 1): the pump never waits for the request pool, and a stream holds no
connection at all. A block is built once per event and shared by every connection; each connection writes one chunk
per tick. Streams do not spread evenly over workers (one usually holds nearly all): nothing here assumes they do.

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
from typing import Callable, Deque, Dict, List, Optional, Protocol, Set

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


class Renderer(Protocol):
    """The messages of one API version, as the bytes from "event:" to the empty line"""

    def event(self, row: OutboxRow, refetch_within_ms: int) -> bytes: ...

    def ready(self) -> bytes: ...

    def heartbeat(self) -> bytes: ...

    def reload(self, refetch_within_ms: int) -> bytes: ...

    def close(self, reason: StreamCloseReason) -> bytes: ...


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
        self.name = f"{HEARTBEAT_KIND}@{socket.gethostname()}:{os.getpid()}"[:64]
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
    async def reload_tree(self) -> None:
        async with self.sessions() as session:
            tree = await load_scope_tree(session)
            await session.rollback()
        self.rights.set_tree(tree)

    async def tick(self) -> int:
        """One round of the pump: every row committed since the last one, delivered. The number of rows"""
        total = 0
        while True:
            head = self.gaps.head
            rows = await read_tick(self.engine, self.gaps, READ_BATCH)
            now = self.clock()
            self.gaps.saw((row.id for row in rows), now)
            self.gaps.expire(now)
            self.position = self.gaps.position(now)
            if any(row.type == EventType.SCOPE_CHANGED.value for row in rows):
                await self.reload_tree()            # before anything of this tick is delivered
            for row in rows:
                self._lags.append(row.lag_ms)
                targets = self._targets(row)
                body = self.render.event(row, self._refetch_window(len(targets)))
                self.ring.add(row, body)
                frame = block(self.position, body)  # built once, shared by every connection
                for connection in targets:
                    connection.offer(frame, row.conflate_key)
            total += len(rows)
            if new_rows_of(rows, head) < READ_BATCH:
                return total

    def _refetch_window(self, subscribers: int) -> int:
        """One event never asks the API for more than refetch_rate_per_second: each worker spreads its own clients, so
        the sum holds whatever the skew (measured: 1,000 refetches at once, p95 5.6 s; over 30 s, p95 126 ms)"""
        window = subscribers * self.workers * 1000 // self.config.refetch_rate_per_second
        return min(window, self.config.refetch_window_max_ms)

    def _may_receive(self, user_id: int, row: OutboxRow) -> bool:
        if row.user_id == user_id:
            return True
        return bool(row.scope_key and row.permission and self.rights.allows(user_id, row.scope_key, row.permission))

    def _targets(self, row: OutboxRow) -> List[Connection]:
        """Connections of the holders of the permission at the scope, plus those of row.user_id, each once"""
        users: Set[int] = set()
        if row.scope_key and row.permission:
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
        self.rights.set_user(user_id, auth.grants)
        retry = random.randint(self.config.retry_min_ms, self.config.retry_max_ms)
        connection.offer(block(self.position, retry_line(retry) + self.render.ready()))
        replay = self._replay(user_id, last_event_id)
        if replay is None:
            self.reloads += 1
            connection.offer(block(self.position, self.render.reload(self._refetch_window(len(self.connections) + 1))))
        else:
            for body in replay:
                connection.offer(block(self.position, body))
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
        for connection in list(self.connections):
            if connection.open and connection.session_id not in alive:
                self._close(connection, StreamCloseReason.SESSION_ENDED)

    async def refresh_rights(self) -> None:
        """The tree, then the grants of the connected users, in one batch on the hub's engine"""
        user_ids = sorted(self.by_user)
        async with self.sessions() as session:
            tree = await load_scope_tree(session)
            users = list((await session.exec(select(User).where(User.id.in_(user_ids)))).all()) if user_ids else []
            grants = await load_grants_of(session, users)
            await session.rollback()
        self.rights.set_tree(tree)
        for user_id in user_ids:
            if user_id in self.by_user:     # still connected after the awaits
                self.rights.set_user(user_id, grants.get(user_id, ()))

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
