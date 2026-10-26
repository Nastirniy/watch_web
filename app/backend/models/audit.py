from typing import Optional, Dict, Any, List
from datetime import datetime, timezone
from sqlalchemy import Index
from sqlmodel import Field, SQLModel, JSON, Column
from app.backend.enums.general import ExportJobType, ExportJobStatus


class AuditLog(SQLModel, table=True):
    """One audited event (enums/audit.py AuditEvent), written by services/audit.create_audit_log.

    event / category: the catalog code and its category (null only on rows a raw INSERT wrote after migration 031
    and before its writer names its event); action / resource_type: the code split at the dot, kept for old readers
    and old rows. params: what the event's message needs, snapshotted at write time. scope_*: the node the event
    happened at (models/role_assignment.scope_columns), all null for a system-wide event. request_id: the
    X-Request-ID of the request (null outside a request). No foreign keys: rows outlive what they name."""
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_category_created", "category", "created_at"),   # purge by category and age
        Index("ix_audit_logs_scope_created", "scope_key", "created_at"),     # scoped reads
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: Optional[int] = Field(default=None, index=True)   # who acted; no foreign key (migration 028)
    session_id: Optional[str] = Field(default=None, max_length=128, index=True)

    event: Optional[str] = Field(default=None, max_length=64, index=True)
    category: Optional[str] = Field(default=None, max_length=24)
    action: str = Field(max_length=100, index=True)
    resource_type: str = Field(max_length=50, index=True)
    resource_id: Optional[int] = Field(default=None, index=True)
    params: Optional[Dict[str, Any]] = Field(default=None, sa_column=Column(JSON))

    scope_key: Optional[str] = Field(default=None, max_length=24)
    scope_post_id: Optional[int] = Field(default=None, index=True)
    scope_group_id: Optional[int] = Field(default=None, index=True)
    request_id: Optional[str] = Field(default=None, max_length=32, index=True)

    old_values: Optional[Dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
    new_values: Optional[Dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
    details: Optional[Dict[str, Any]] = Field(default=None, sa_column=Column(JSON))

    ip_address: Optional[str] = Field(default=None, max_length=45)
    # AutoString was VARCHAR(255) on MySQL already; the length is explicit so that the cut can read it
    user_agent: Optional[str] = Field(default=None, max_length=255)
    request_url: Optional[str] = Field(default=None, max_length=500)

    success: bool = Field(default=True, index=True)
    error_message: Optional[str] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), index=True)


class ExportJob(SQLModel, table=True):
    __tablename__ = "export_jobs"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    job_type: str = Field(max_length=20, index=True)

    date_from: Optional[datetime] = Field(default=None)
    date_to: Optional[datetime] = Field(default=None)
    post_ids: Optional[List[int]] = Field(default=None, sa_column=Column(JSON))
    signal_type_ids: Optional[List[int]] = Field(default=None, sa_column=Column(JSON))

    status: str = Field(default=ExportJobStatus.PENDING.value, max_length=15, index=True)
    output_filename: Optional[str] = Field(default=None, max_length=255)
    error_message: Optional[str] = Field(default=None)

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc), index=True)
    started_at: Optional[datetime] = Field(default=None)
    completed_at: Optional[datetime] = Field(default=None)
    expires_at: Optional[datetime] = Field(default=None, index=True)