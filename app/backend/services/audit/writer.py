"""create_audit_log: the one writer of audit rows (raw INSERTs aside: the restore tool and frozen migrations)."""
import sys
from typing import Any, Dict, Optional

from pydantic_core import to_jsonable_python
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.auth.scope import Node
from app.backend.enums.audit import AuditEvent
from app.backend.errors.coded import is_optional
from app.backend.models.audit import AuditLog
from app.backend.models.role_assignment import scope_columns
from app.backend.services.audit import legacy
from app.backend.utils.logging_config import request_id_context
from app.backend.utils.text import fit_column

NO_SCOPE = {"scope_group_id": None, "scope_post_id": None, "scope_key": None}


def serialize_for_audit(obj: Any) -> Any:
    """
    Recursively convert non-serializable objects to JSON-compatible ones using Pydantic's logic.
    Handles Decimal, datetime, UUID, Enums, etc.
    """
    if obj is None:
        return None
    return to_jsonable_python(obj)


def check_event_params(event: AuditEvent, params: Dict[str, Any]) -> None:
    """Params must be exactly the ones the event declares, required ones present (the rule of problem params)"""
    unknown = set(params) - set(event.params)
    missing = {name for name, annotation in event.params.items() if not is_optional(annotation) and name not in params}
    if unknown or missing:
        raise TypeError(f"{event.code}: unknown params {sorted(unknown)}, missing {sorted(missing)}")


async def create_audit_log(
    session: AsyncSession,
    event: Optional[AuditEvent] = None,
    *,
    params: Optional[Dict[str, Any]] = None,
    scope: Optional[Node] = None,
    resource_id: Optional[int] = None,
    success: bool = True,
    user_id: Optional[int] = None,
    session_id: Optional[str] = None,
    old_values: Optional[Dict] = None,
    new_values: Optional[Dict] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    request_url: Optional[str] = None,
    workspace: Optional[Node] = None,
    action: Optional[str] = None,
    resource_type: Optional[str] = None,
    details: Optional[Dict] = None,
) -> Optional[AuditLog]:
    """Add the audit row of an event to the session of the change (the caller commits; a failure to write it fails
    the request).

    event: the catalog event (enums/audit.py); its code gives resource_type / action, its category the category.
    params: exactly the event's params (unknown or missing required one: TypeError; Optional ones may be left out),
    snapshotted now. scope: the node the event happened at (None: system-wide). The request id comes from the
    request's context. workspace is accepted and ignored until its column exists (part 5).

    Legacy form, until every writer names its event: action= and resource_type= (and details=) instead of the
    event, mapped through migrations/support/audit_events_v1.py LEGACY_EVENTS; details is still written for it.

    Returns the row (already added to the session); the switches of phase B make it None for a category that is off.
    """
    if action is not None or resource_type is not None or details is not None:
        if event is not None or params is not None or action is None or resource_type is None:
            raise TypeError("create_audit_log takes either an AuditEvent with params or the legacy "
                            "action= / resource_type= (details=)")
        legacy.warn_once(sys._getframe(1), action, resource_type)
        event, params = legacy.legacy_event(action, resource_type, details)
    else:
        if not isinstance(event, AuditEvent):
            raise TypeError(f"create_audit_log needs an AuditEvent, not {event!r}")
        check_event_params(event, params or {})
        resource_type, action = event.code.split(".", 1)
    audit = AuditLog(
        event=event.code,
        category=event.category.value,
        user_id=user_id,
        session_id=session_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        params=serialize_for_audit(params),
        old_values=serialize_for_audit(old_values),
        new_values=serialize_for_audit(new_values),
        details=serialize_for_audit(details),
        ip_address=ip_address,
        user_agent=fit_column(AuditLog.user_agent, user_agent),
        request_url=fit_column(AuditLog.request_url, request_url),
        request_id=fit_column(AuditLog.request_id, request_id_context.get()),
        success=success,
        **(scope_columns(scope) if scope is not None else NO_SCOPE),
    )
    session.add(audit)
    return audit
