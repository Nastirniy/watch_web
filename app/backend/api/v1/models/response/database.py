from typing import List, Optional

from pydantic import BaseModel, Field

from app.backend.utils.validators import ResponseDatetime


class DatabaseServerResponse(BaseModel):
    product: str = Field(description="mariadb, mysql or sqlite")
    version: str = Field(description="Server version string")
    uptime_seconds: Optional[int] = Field(None, description="Seconds since the server started; null on sqlite")
    threads_connected: Optional[int] = Field(None, description="Open connections now")
    max_used_connections: Optional[int] = Field(None, description="Most connections open at once since the start")
    max_connections: Optional[int] = Field(None, description="Connection limit of the server")


class DatabaseTableResponse(BaseModel):
    name: str
    rows_estimate: Optional[int] = Field(None, description="Estimated rows (InnoDB statistics, not an exact count)")
    data_bytes: Optional[int] = Field(None, description="Size of the rows")
    index_bytes: Optional[int] = Field(None, description="Size of the indexes")


class DatabaseSchemaResponse(BaseModel):
    name: str = Field(description="Database (schema) name")
    migration_revision: Optional[str] = Field(None, description="Alembic revision of the database")
    code_revision: str = Field(description="Newest Alembic revision this version of the application knows")
    up_to_date: bool = Field(description="migration_revision equals code_revision")
    size_bytes: Optional[int] = Field(None, description="Data and indexes of every table")
    tables: List[DatabaseTableResponse] = Field(description="Largest first")


class DatabaseStatusResponse(BaseModel):
    server: DatabaseServerResponse
    schema_: DatabaseSchemaResponse = Field(alias="schema", serialization_alias="schema")

    model_config = {"populate_by_name": True}


class BackupLastRunResponse(BaseModel):
    outcome: str = Field(description="ok or failed")
    at: ResponseDatetime = Field(description="When the run ended")
    error_code: Optional[str] = Field(None, description="Why it failed (no_space, dump_failed, timeout, "
                                                        "copy_failed, ...); null when ok")


class BackupCopyResponse(BaseModel):
    configured: bool = Field(description="An off-host copy directory is set")
    reachable: bool = Field(description="The copy directory can be read")
    missing: int = Field(description="Backups without a valid copy")


class BackupSummaryResponse(BaseModel):
    last_success_at: Optional[ResponseDatetime] = Field(None, description="End of the newest complete backup")
    last_success_id: Optional[str] = Field(None, description="Id of the newest complete backup")
    age_seconds: Optional[int] = Field(None, description="Seconds since last_success_at")
    stale: bool = Field(description="No complete backup newer than stale_after_hours")
    stale_after_hours: int
    last_run: Optional[BackupLastRunResponse] = Field(None, description="Outcome of the last backup run")
    count: int = Field(description="Complete backups")
    total_size_bytes: int
    free_bytes: int = Field(description="Free space in the backup directory")
    copy_: BackupCopyResponse = Field(alias="copy", serialization_alias="copy")

    model_config = {"populate_by_name": True}


class BackupResponse(BaseModel):
    id: str = Field(description="UTC start time and label, e.g. 20261022T031500Z-scheduled")
    label: str = Field(description="scheduled, manual or pre-upgrade")
    started_at: ResponseDatetime
    duration_seconds: int
    size_bytes: int
    sha256: str = Field(description="SHA-256 of the compressed dump")
    migration_revision: Optional[str] = Field(None, description="Alembic revision of the data in the backup")
    copied: Optional[bool] = Field(None, description="A valid copy exists in the copy directory; null without one")


class PreviousStateResponse(BaseModel):
    schema_: str = Field(alias="schema", serialization_alias="schema",
                         description="Schema holding the tables a restore replaced (undo-restore brings them back)")
    created_at: ResponseDatetime
    size_bytes: int

    model_config = {"populate_by_name": True}


class BackupsResponse(BaseModel):
    summary: BackupSummaryResponse
    backups: List[BackupResponse] = Field(description="Newest first")
    previous_states: List[PreviousStateResponse] = Field(description="Kept schemas, oldest first")
