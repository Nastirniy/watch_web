"""The seam for a broker. Routes and services know publish() and the hub; only this module knows that today the bus is
the outbox table. A relay to Redis Streams or NATS (stage 2) is another EventBus: publish stays in the transaction of
the change, subscribe gives the hub its rows. Two methods, on purpose."""
from typing import Any, List, Optional, Protocol

from sqlalchemy.ext.asyncio import AsyncEngine
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.auth.scope import Node
from app.backend.enums.events import EventType
from app.backend.enums.general import Permissions
from app.backend.services.events.publish import publish as publish_event
from app.backend.services.events.gaps import GapTracker
from app.backend.services.events.reader import OutboxRow, read_tick


class EventBus(Protocol):
    def publish(self, session: AsyncSession, event: EventType, *, scope: Optional[Node] = None,
                permission: Optional[Permissions] = None, user_id: Optional[int] = None,
                conflate_key: Optional[str] = None, **params: Any) -> None:
        """Add the event to the transaction of the change"""

    async def subscribe(self, tracker: GapTracker) -> List[OutboxRow]:
        """The rows committed since the last call, late commits included (at least once)"""


class OutboxBus:
    """Stage 1: the table outbox_events, read by every api worker"""

    def __init__(self, engine: AsyncEngine, batch: int = 1000):
        self.engine, self.batch = engine, batch

    def publish(self, session: AsyncSession, event: EventType, **kwargs: Any) -> None:
        publish_event(session, event, **kwargs)

    async def subscribe(self, tracker: GapTracker) -> List[OutboxRow]:
        return await read_tick(self.engine, tracker, self.batch)
