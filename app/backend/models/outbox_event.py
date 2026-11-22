"""One row per event to the web client: delivery data, never a record (deleted after events.retention_seconds)."""
from datetime import datetime
from typing import Optional

from sqlalchemy import BigInteger, Column, DateTime, Integer, func
from sqlmodel import Field, SQLModel


class OutboxEvent(SQLModel, table=True):
    """Written in the transaction of the change (services/events/publish.py), read by the hub of every api worker
    (services/events/reader.py) and deleted by the media worker (services/events/cleanup.py).

    No foreign key: it would lock the parent row (migration 028). No index but the primary key: every insert pays for
    each one and nothing needs one. Migration 037 creates id as BIGINT UNSIGNED and occurred_at as DATETIME(6)."""
    __tablename__ = "outbox_events"

    id: Optional[int] = Field(default=None, sa_column=Column(
        BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True))
    type: str = Field(max_length=48)                        # EventType
    v: int = Field(default=1)                               # payload version of the type
    scope_key: Optional[str] = Field(default=None, max_length=24)     # root, g:<id>, p:<id>; null: personal only
    permission: Optional[str] = Field(default=None, max_length=48)    # who may receive it at the scope
    user_id: Optional[int] = Field(default=None)            # also this user, whatever their rights
    conflate_key: Optional[str] = Field(default=None, max_length=48)  # only the latest of a key waits in a queue
    payload: str = Field(max_length=512)                    # JSON of the catalog's typed params
    occurred_at: Optional[datetime] = Field(default=None, sa_column=Column(
        DateTime(timezone=False), nullable=False, server_default=func.now()))
