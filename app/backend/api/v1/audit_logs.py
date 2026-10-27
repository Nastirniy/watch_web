"""The audit log, read-only: the entries (one model per event), one entry, the event catalog. audit.view (system)
reads every row and every column; audit.view_scope (tree) reads the rows of the nodes where it is in effect, without
the device fields (services/audit/query.py visibility_of)."""
from typing import Annotated, List

from fastapi import APIRouter, Depends, Query
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.api.v1.models.query.audit import AuditFilterParams
from app.backend.api.v1.models.response.audit import (
    AuditEntryResponse, AuditEventResponse, audit_entries_to_response, audit_entry_to_response,
)
from app.backend.auth.access_control import AccessControlService
from app.backend.auth.access_errors import access_denied
from app.backend.auth.context import AuthContext
from app.backend.auth.require import Require, require
from app.backend.auth.scope import Node
from app.backend.database import get_session
from app.backend.enums.audit import SWITCHABLE, AuditEvent
from app.backend.enums.errors import AccessErrorCode, ErrorCode
from app.backend.enums.general import Permissions
from app.backend.enums.routes import AuditRoutes
from app.backend.errors.openapi import GUARDED, problems
from app.backend.errors.problems import ApiError
from app.backend.services.audit.query import (
    AuditFilterService, AuditVisibility, entry_exists, get_entry, list_entries, visibility_of,
)

router = APIRouter(prefix=AuditRoutes.BASE.value, tags=[AuditRoutes.tag()])

READERS = (Permissions.AUDIT_VIEW, Permissions.AUDIT_VIEW_SCOPE)
# The node filters of a scoped reader outside its scope
SCOPE_FILTER_DENIED = (AccessErrorCode.POST_ACCESS_DENIED, AccessErrorCode.NO_POST_ASSIGNED,
                       AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION)


def check_scope_filters(auth: AuthContext, visibility: AuditVisibility, filters: AuditFilterParams) -> None:
    """A scoped reader filters only inside its scope: post_id where audit.view_scope is in effect (check_post),
    group_id / corps_id / brigade_id inside a subtree where it is held (check_covers), otherwise 403. The system
    view filters anywhere."""
    if visibility.everything:
        return
    if filters.post_id is not None:
        AccessControlService.check_post(auth, Permissions.AUDIT_VIEW_SCOPE, filters.post_id, resource_type="audit")
    for group_id in (filters.group_id, filters.corps_id, filters.brigade_id):
        if group_id is not None:
            AccessControlService.check_covers(auth, Permissions.AUDIT_VIEW_SCOPE, Node.group(group_id),
                                              "list_audit_entries")


@router.get(AuditRoutes.ROOT.value, response_model=List[AuditEntryResponse],
            responses=problems(*SCOPE_FILTER_DENIED, *GUARDED))
async def list_audit_entries(
        filters: Annotated[AuditFilterParams, Query()],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(any=READERS)),
):
    """Audit entries, newest first; the next page: before_id = the id of the last entry. params of each entry are
    typed by its event; legacy.unknown is a row written before the event catalog.

    audit.view: every entry. audit.view_scope: the entries of the posts where it is in effect and of the groups
    inside the subtrees where it is held (the root ones only held at the root; system entries never), the device
    fields null; post_id / group_id / corps_id / brigade_id outside them answer 403."""
    visibility = visibility_of(auth)
    check_scope_filters(auth, visibility, filters)
    entries = await list_entries(session, AuditFilterService.criteria(filters), visibility, auth.tree)
    return audit_entries_to_response(entries)


@router.get(AuditRoutes.EVENTS.value, response_model=List[AuditEventResponse], responses=problems(*GUARDED))
async def list_audit_events(
        auth: AuthContext = Depends(require(any=READERS)),
):
    """The event catalog: every code with its category and whether the category can be switched off"""
    return [AuditEventResponse(code=event.code, category=event.category, switchable=event.category in SWITCHABLE,
                               title=event.title) for event in AuditEvent]


@router.get(AuditRoutes.ENTRY_BY_ID.value, response_model=AuditEntryResponse,
            responses=problems(ErrorCode.AUDIT_ENTRY_NOT_FOUND, AccessErrorCode.AUDIT_ENTRY_ACCESS_DENIED, *GUARDED))
async def get_audit_entry(
        audit_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(any=READERS)),
):
    """One audit entry; one that exists outside the caller's scope (audit.view_scope) answers 403"""
    visibility = visibility_of(auth)
    entry = await get_entry(session, audit_id, visibility)
    if entry is not None:
        return audit_entry_to_response(entry)
    if await entry_exists(session, audit_id):
        names = [permission.value for permission in READERS]
        raise access_denied(AccessErrorCode.AUDIT_ENTRY_ACCESS_DENIED, required=Require.of(any=READERS),
                            missing=names, audit_id=audit_id)
    raise ApiError(ErrorCode.AUDIT_ENTRY_NOT_FOUND, audit_id=audit_id)
