"""The audit log, read-only: the entries (one model per event), one entry, the event catalog. audit.view (system)
reads every row and every column."""
from typing import Annotated, List

from fastapi import APIRouter, Depends, Query
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.api.v1.models.query.audit import AuditFilterParams
from app.backend.api.v1.models.response.audit import (
    AuditEntryResponse, AuditEventResponse, audit_entries_to_response, audit_entry_to_response,
)
from app.backend.auth.context import AuthContext
from app.backend.auth.require import require
from app.backend.database import get_session
from app.backend.enums.audit import SWITCHABLE, AuditEvent
from app.backend.enums.errors import ErrorCode
from app.backend.enums.general import Permissions
from app.backend.enums.routes import AuditRoutes
from app.backend.errors.openapi import GUARDED, problems
from app.backend.errors.problems import ApiError
from app.backend.services.audit.query import AuditFilterService, get_entry, list_entries, visibility_of

router = APIRouter(prefix=AuditRoutes.BASE.value, tags=[AuditRoutes.tag()])


@router.get(AuditRoutes.ROOT.value, response_model=List[AuditEntryResponse], responses=problems(*GUARDED))
async def list_audit_entries(
        filters: Annotated[AuditFilterParams, Query()],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.AUDIT_VIEW])),
):
    """Audit entries, newest first; the next page: before_id = the id of the last entry. params of each entry are
    typed by its event; legacy.unknown is a row written before the event catalog."""
    entries = await list_entries(session, AuditFilterService.criteria(filters), visibility_of(auth), auth.tree)
    return audit_entries_to_response(entries)


@router.get(AuditRoutes.EVENTS.value, response_model=List[AuditEventResponse], responses=problems(*GUARDED))
async def list_audit_events(
        auth: AuthContext = Depends(require(all=[Permissions.AUDIT_VIEW])),
):
    """The event catalog: every code with its category and whether the category can be switched off"""
    return [AuditEventResponse(code=event.code, category=event.category, switchable=event.category in SWITCHABLE,
                               title=event.title) for event in AuditEvent]


@router.get(AuditRoutes.ENTRY_BY_ID.value, response_model=AuditEntryResponse,
            responses=problems(ErrorCode.AUDIT_ENTRY_NOT_FOUND, *GUARDED))
async def get_audit_entry(
        audit_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.AUDIT_VIEW])),
):
    """One audit entry"""
    entry = await get_entry(session, audit_id, visibility_of(auth))
    if entry is None:
        raise ApiError(ErrorCode.AUDIT_ENTRY_NOT_FOUND, audit_id=audit_id)
    return audit_entry_to_response(entry)
