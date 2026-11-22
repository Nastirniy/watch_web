"""What the hubs reported: their rows of worker_heartbeats (kind events, details a compact JSON of counts), read by
/health. A request lands on one worker and knows only its own hub, so the counts come from the table."""
import json
from datetime import datetime, timedelta, timezone
from typing import Dict, List

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.models.worker import WorkerHeartbeat
from app.backend.services.events.hub import HEARTBEAT_KIND
from app.backend.utils.datetime_utils import assume_utc

COUNTS = ("streams", "lag_ms", "gaps", "slow_closed", "reloads")


async def hub_reports(session: AsyncSession, within_seconds: int) -> List[Dict[str, int]]:
    """The counts of every hub seen within the last within_seconds; a row whose details are not ours counts zero"""
    since = datetime.now(timezone.utc) - timedelta(seconds=within_seconds)
    rows = (await session.exec(select(WorkerHeartbeat).where(WorkerHeartbeat.kind == HEARTBEAT_KIND))).all()
    reports = []
    for row in rows:
        if row.last_seen_at is None or assume_utc(row.last_seen_at) < since:
            continue
        try:
            details = json.loads(row.details or "{}")
        except ValueError:
            details = {}
        if not isinstance(details, dict):
            details = {}
        reports.append({name: int(details[name]) if isinstance(details.get(name), int) else 0 for name in COUNTS})
    return reports
