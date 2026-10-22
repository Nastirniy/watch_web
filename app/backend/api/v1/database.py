"""Database and backup status, read-only. Backups are made and restored on the host by cli/db_backup.py (the
db-backup tool service, BACKUPS.md); the API only reads the backup directories (mounted read-only)."""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.api.v1.models.response.database import (
    BackupResponse, BackupsResponse, DatabaseStatusResponse, PreviousStateResponse,
)
from app.backend.auth.context import AuthContext
from app.backend.auth.require import require
from app.backend.config import settings
from app.backend.database import get_session
from app.backend.enums.errors import ErrorCode
from app.backend.enums.general import Permissions
from app.backend.enums.routes import DatabaseRoutes
from app.backend.errors.coded import CodedError
from app.backend.errors.openapi import GUARDED, problems
from app.backend.errors.problems import ApiError
from app.backend.services.backups.catalog import load_catalog_async
from app.backend.services.backups.schemas import kept_pattern, kept_schemas_query, parse_kept
from app.backend.services.database_status import database_status

router = APIRouter(prefix=DatabaseRoutes.BASE.value, tags=[DatabaseRoutes.tag()])


@router.get(DatabaseRoutes.STATUS.value, response_model=DatabaseStatusResponse, responses=problems(*GUARDED))
async def get_database_status(
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.DATABASE_VIEW])),
):
    """The database server (version, uptime, connections) and the schema (migration revision, size, tables)"""
    return DatabaseStatusResponse.model_validate(await database_status(session), from_attributes=True)


async def _previous_states(session: AsyncSession) -> list:
    conn = await session.connection()
    if conn.dialect.name == "sqlite":
        return []
    name = settings.database.name
    rows = (await conn.execute(kept_schemas_query(), {"pattern": kept_pattern(name)})).all()
    return parse_kept(name, [tuple(row) for row in rows])


@router.get(
    DatabaseRoutes.BACKUPS.value,
    response_model=BackupsResponse,
    responses=problems(ErrorCode.BACKUPS_NOT_CONFIGURED, ErrorCode.BACKUP_DIRECTORY_UNAVAILABLE, *GUARDED),
)
async def get_database_backups(
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.BACKUP_VIEW])),
):
    """The backups in the backup directory, newest first, whether each has an off-host copy, the summary (newest
    good backup, stale, the last run) and the schemas a restore kept for undo"""
    try:
        catalog = await load_catalog_async(settings.backup, datetime.now(timezone.utc))
    except CodedError as exc:
        raise ApiError.from_coded(exc)
    summary = catalog.summary
    return BackupsResponse(
        summary={
            "last_success_at": summary.last_success_at, "last_success_id": summary.last_success_id,
            "age_seconds": summary.age_seconds, "stale": summary.stale,
            "stale_after_hours": summary.stale_after_hours,
            "last_run": None if summary.last_run is None else {
                "outcome": summary.last_run.outcome, "at": summary.last_run.finished_at,
                "error_code": summary.last_run.error_code},
            "count": summary.count, "total_size_bytes": summary.total_size_bytes, "free_bytes": summary.free_bytes,
            "copy": {"configured": summary.copy.configured, "reachable": summary.copy.reachable,
                     "missing": summary.copy.missing},
        },
        backups=[BackupResponse(
            id=e.manifest.id, label=e.manifest.label.value, started_at=e.manifest.started_at,
            duration_seconds=int((e.manifest.finished_at - e.manifest.started_at).total_seconds()),
            size_bytes=e.manifest.size_bytes, sha256=e.manifest.sha256,
            migration_revision=e.manifest.migration_revision, copied=e.copied) for e in catalog.entries],
        previous_states=[PreviousStateResponse(schema=k.name, created_at=k.created_at, size_bytes=k.size_bytes)
                         for k in await _previous_states(session)],
    )
