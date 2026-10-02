from typing import Optional
from datetime import datetime
from sqlmodel import Field, SQLModel
from app.backend.utils.datetime_utils import get_default_timezone_datetime


class WorkerHeartbeat(SQLModel, table=True):
    """One row per running background worker process, refreshed every poster.heartbeat_seconds.

    Lets the API report whether the media worker is alive without any RPC
    between the two processes.
    """
    __tablename__ = "worker_heartbeats"

    name: str = Field(primary_key=True, max_length=64)  # "{kind}@{hostname}:{pid}"
    kind: str = Field(max_length=32, index=True)
    hostname: str = Field(max_length=128)
    pid: int
    started_at: datetime = Field(default_factory=get_default_timezone_datetime)
    last_seen_at: datetime = Field(default_factory=get_default_timezone_datetime)
    jobs_done: int = Field(default=0)
    jobs_failed: int = Field(default=0)
    details: Optional[str] = Field(default=None, max_length=500)
