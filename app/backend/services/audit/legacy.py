"""The legacy keyword form of create_audit_log: action=, resource_type=, details= (the writers before the event
catalog). Accepted only until every writer names its AuditEvent (part 6, Task 3), then removed.

The pair maps through the frozen migrations/support/audit_events_v1.py LEGACY_EVENTS, the mapping migration 031
backfilled the old rows with; a pair it does not list is legacy.unknown. One warning per call site, not per row."""
from types import FrameType
from typing import Any, Dict, Optional, Set, Tuple

from loguru import logger

from app.backend.enums.audit import AuditEvent
from app.backend.migrations.support.audit_events_v1 import LEGACY_EVENTS, LEGACY_UNKNOWN

# (file, line) of every call site already warned about
_warned: Set[Tuple[str, int]] = set()


def legacy_event(action: str, resource_type: str,
                 details: Optional[Dict[str, Any]]) -> Tuple[AuditEvent, Optional[Dict[str, Any]]]:
    """(event, params) of an old-style pair: params only for legacy.unknown, what its message needs"""
    code, _category = LEGACY_EVENTS.get((resource_type, action), LEGACY_UNKNOWN)
    event = AuditEvent(code)
    if event is not AuditEvent.LEGACY_UNKNOWN:
        return event, None
    params: Dict[str, Any] = {"action": action, "resource_type": resource_type}
    message = details.get("message") if isinstance(details, dict) else None
    if isinstance(message, str):
        params["message"] = message
    return event, params


def warn_once(caller: Optional[FrameType], action: str, resource_type: str) -> None:
    site = (caller.f_code.co_filename, caller.f_lineno) if caller is not None else ("?", 0)
    if site in _warned:
        return
    _warned.add(site)
    logger.warning(f"create_audit_log(action={action!r}, resource_type={resource_type!r}) at {site[0]}:{site[1]}: "
                   f"the legacy form, name the AuditEvent")
