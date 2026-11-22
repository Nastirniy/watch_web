"""The start of a hub, best effort: a hub that fails to start never stops the API.

The first attempt runs in the lifespan, as before. When it fails, the error is logged with its traceback, the API
starts anyway, and the attempt is repeated in the background (5 s, doubling to 5 min) until the hub runs; then
on_started hands it over (app/main.py: the SIGTERM drain, app.state). Until then the stream route answers 503
event_stream_capacity with a long Retry-After, and this process keeps a row of its own in worker_heartbeats (kind
events, details {"down": 1}, the hub's name) every report interval, which /health counts as a worker whose hub is
down; the hub's first report replaces it.

A start can fail with anything (the database, a migration not yet applied, a bug): the loop catches Exception, logs and
retries (the allowed broad catch of a worker loop). A repeat of the same kind of failure is one line without a
traceback. No FastAPI here: app/main.py builds the hub (make_hub) and keeps it.
"""
import asyncio
import json
import os
import socket
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from loguru import logger
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.backend.services.events.hub import HEARTBEAT_KIND, REPORT_SECONDS, EventHub, hub_name
from app.backend.services.media import jobs

RETRY_FIRST_SECONDS = 5.0
RETRY_MAX_SECONDS = 300.0
DOWN_DETAILS = json.dumps({"down": 1}, separators=(",", ":"))


class HubStarter:
    def __init__(self, make_hub: Callable[[], Awaitable[EventHub]], on_started: Callable[[EventHub], None],
                 sessions: Callable[[], Awaitable[async_sessionmaker]], *, first_delay: Optional[float] = None,
                 max_delay: float = RETRY_MAX_SECONDS, report_seconds: float = REPORT_SECONDS,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep):
        """make_hub: a started hub, or an exception after cleaning up what it opened (its engine). sessions: the
        session maker of the down row (the request pool: the hub's own engine is what may be failing)"""
        self.make_hub = make_hub
        self.on_started = on_started
        self.sessions = sessions
        self.first_delay = RETRY_FIRST_SECONDS if first_delay is None else first_delay
        self.max_delay = max_delay
        self.report_seconds = report_seconds
        self.sleep = sleep
        self.hub: Optional[EventHub] = None
        self.task: Optional[asyncio.Task] = None
        self.failures = 0
        self._last_failure: Optional[type] = None
        self.name = hub_name()
        self.started_at = datetime.now(timezone.utc)

    async def start(self) -> Optional[EventHub]:
        """One attempt now; when it fails, the retry loop in the background. The hub, or None while it is down"""
        if await self._attempt():
            return self.hub
        await self._report_down()
        self.task = asyncio.create_task(self._retry(), name="events-hub-start")
        return None

    async def stop(self) -> None:
        """Cancel the retry loop (a start under way included), stop a hub that started, or remove the down row"""
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None
        if self.hub is not None:
            await self.hub.stop()
            self.hub = None
        elif self.failures:
            await self._remove_down()

    async def _attempt(self) -> bool:
        try:
            hub = await self.make_hub()
        except asyncio.CancelledError:
            raise
        except Exception as exc:        # a start loop: whatever the start failed with is logged and retried
            self._failed(exc)
            return False
        self.hub = hub
        if self.failures:
            logger.info(f"Event hub started after {self.failures} failed attempt(s): streams are served again")
        self.on_started(hub)
        return True

    def _failed(self, exc: Exception) -> None:
        self.failures += 1
        if type(exc) is not self._last_failure:
            logger.opt(exception=exc).error(
                f"Event hub failed to start (attempt {self.failures}): this process answers event streams with 503 "
                f"until it starts; retrying in the background")
        else:
            logger.warning(f"Event hub failed to start again (attempt {self.failures}): {type(exc).__name__}")
        self._last_failure = type(exc)

    async def _retry(self) -> None:
        delay = self.first_delay
        while True:
            await self._wait(delay)
            if await self._attempt():
                return
            await self._report_down()
            delay = min(delay * 2, self.max_delay)

    async def _wait(self, seconds: float) -> None:
        """Wait in report intervals and refresh the down row between them: /health reads rows of the last 60 s"""
        remaining = seconds
        while remaining > 0:
            step = min(self.report_seconds, remaining)
            await self.sleep(step)
            remaining -= step
            if remaining > 0:
                await self._report_down()

    async def _report_down(self) -> None:
        """Best effort: the database may well be what keeps the hub from starting (a probe: logged at debug)"""
        try:
            maker = await self.sessions()
            async with maker() as session:
                await jobs.heartbeat(session, name=self.name, kind=HEARTBEAT_KIND, hostname=socket.gethostname()[:128],
                                     pid=os.getpid(), started_at=self.started_at, jobs_done=0,
                                     jobs_failed=self.failures, details=DOWN_DETAILS)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("Event hub starter: the down row was not written")

    async def _remove_down(self) -> None:
        try:
            maker = await self.sessions()
            async with maker() as session:
                await jobs.remove_heartbeat(session, self.name)
        except asyncio.CancelledError:
            raise
        except Exception:               # best effort as the row: it ages out of /health in 60 s anyway
            logger.debug("Event hub starter: the down row was not removed")
