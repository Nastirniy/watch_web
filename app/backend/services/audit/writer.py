"""create_audit_log: the one writer of audit rows (raw INSERTs aside: the restore tool and frozen migrations)."""
from typing import Any, Dict, Optional

from pydantic_core import to_jsonable_python
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.auth.scope import Node
from app.backend.enums.audit import SERVED_ONLY, AuditEvent
from app.backend.errors.coded import is_optional
from app.backend.models.audit import AuditLog
from app.backend.models.role_assignment import scope_columns
from app.backend.utils.logging_config import request_id_context
from app.backend.utils.text import fit_column

NO_SCOPE = {"scope_group_id": None, "scope_post_id": None, "scope_key": None}

# A search event (search.<entity>) keeps action "search" with the singular entity as resource_type; every other
# event writes its code split at the dot. Search rows written before the catalog keep their stored PLURAL
# resource_type (migration 031 backfills event / category only and never rewrites resource_type).
SEARCH_ACTION = "search"
SEARCHED_RESOURCE = {
    "observations": "observation", "users": "user", "posts": "post", "roles": "role", "categories": "category",
    "signal_types": "signal_type", "tags": "tag", "media": "media", "media_formats": "media_format",
}


def resource_and_action(event: AuditEvent) -> tuple[str, str]:
    """(resource_type, action) columns of a new-form row of the event"""
    resource_type, action = event.code.split(".", 1)
    if resource_type == SEARCH_ACTION:
        return SEARCHED_RESOURCE[action], SEARCH_ACTION
    return resource_type, action


def serialize_for_audit(obj: Any) -> Any:
    """
    Recursively convert non-serializable objects to JSON-compatible ones using Pydantic's logic.
    Handles Decimal, datetime, UUID, Enums, etc.
    """
    if obj is None:
        return None
    return to_jsonable_python(obj)


def present_params(event: AuditEvent, params: Dict[str, Any]) -> Dict[str, Any]:
    """The params as stored: an Optional param passed as None is left out (absent, never null); None for a required
    param is a TypeError (a row would hold a value its event says is always there)"""
    nulls = sorted(name for name, value in params.items()
                   if value is None and name in event.params and not is_optional(event.params[name]))
    if nulls:
        raise TypeError(f"{event.code}: required params {nulls} are None")
    # an unknown name stays, None or not: check_event_params refuses it
    return {name: value for name, value in params.items() if value is not None or name not in event.params}


def check_event_params(event: AuditEvent, params: Dict[str, Any]) -> None:
    """Params must be exactly the ones the event declares, required ones present (the rule of problem params)"""
    unknown = set(params) - set(event.params)
    missing = {name for name, annotation in event.params.items() if not is_optional(annotation) and name not in params}
    if unknown or missing:
        raise TypeError(f"{event.code}: unknown params {sorted(unknown)}, missing {sorted(missing)}")


async def create_audit_log(
    session: AsyncSession,
    event: AuditEvent,
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
) -> Optional[AuditLog]:
    """Add the audit row of an event to the session of the change (the caller commits; a failure to write it fails
    the request).

    event: the catalog event (enums/audit.py); its code gives resource_type / action (resource_and_action), its
    category the category.
    params: exactly the event's params (unknown or missing required one: TypeError; Optional ones may be left out, and
    one passed as None is left out; a required one passed as None: TypeError), snapshotted now. The served-only events
    (SERVED_ONLY: legacy.unknown, legacy.migrate) are refused (TypeError).
    scope: the node the event happened at (None: system-wide). The request id comes from the request's context.
    workspace is accepted and ignored until its column exists (part 5).

    Returns the row (already added to the session); the switches of phase B make it None for a category that is off.
    """
    if not isinstance(event, AuditEvent):
        raise TypeError(f"create_audit_log needs an AuditEvent, not {event!r}")
    if event in SERVED_ONLY:
        raise TypeError(f"{event.code} is served by the read API, never written")
    if params is not None:
        params = present_params(event, params)
    check_event_params(event, params or {})
    resource_type, action = resource_and_action(event)
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
        ip_address=ip_address,
        user_agent=fit_column(AuditLog.user_agent, user_agent),
        request_url=fit_column(AuditLog.request_url, request_url),
        request_id=fit_column(AuditLog.request_id, request_id_context.get()),
        success=success,
        **(scope_columns(scope) if scope is not None else NO_SCOPE),
    )
    session.add(audit)
    return audit
