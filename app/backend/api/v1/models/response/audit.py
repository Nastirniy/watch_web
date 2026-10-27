"""Audit log entries in v1: one model per event of the catalog (enums/audit.py), built like the problem models
(errors/openapi.py problem_models): <CamelCode>AuditEntry with event: Literal[code] and params: <CamelCode>AuditParams
(typed params; an optional param is absent, never null). AuditEntryResponse is their union, discriminated by event,
so openapi-typescript narrows params by event. Rows the catalog cannot describe (no event, an unknown code, params
that do not fit the event: rows written before the catalog) are served as legacy.unknown, with the stored code in
params.stored_event. Values are served as stored: these models never strip or unescape strings."""
import dataclasses
from functools import lru_cache
from typing import Annotated, Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model, model_serializer

from app.backend.api.v1.models.query.audit import AuditEventCode
from app.backend.auth.scope import NodeKind
from app.backend.enums.audit import AuditCategory, AuditEvent, audit_event
from app.backend.errors.coded import is_optional
from app.backend.errors.openapi import absent_not_null
from app.backend.services.audit.query import AuditEntry
from app.backend.utils.validators import ResponseDatetime

_CODE_DOC = "Key of exactly this message in the localization package"
_DEVICE = "null without audit.view (it identifies a device)"


class AuditModel(BaseModel):
    """Base of the audit response models: values as stored (no strip, no unescape: a record is never rewritten)"""
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class AuditActorResponse(AuditModel):
    """Who acted"""
    id: int
    username: Optional[str] = Field(description="The username now; null for an id no user has")


class AuditScopeResponse(AuditModel):
    """The node of the scope tree the event happened at"""
    kind: NodeKind
    id: Optional[int] = Field(description="Id of the group or the post; null at the root")
    name: Optional[str] = Field(description="Name of the group or the post now; the application name at the root")


class AuditParamsBase(BaseModel):
    """What the message of the event needs, as it was when the row was written"""
    model_config = ConfigDict(extra="ignore", json_schema_extra=lambda schema, cls: absent_not_null(schema))

    @model_serializer(mode="wrap")
    def _absent_not_null(self, handler):
        return {name: value for name, value in handler(self).items() if value is not None}


class AuditEntryBase(AuditModel):
    """The fields every audit entry has"""
    id: int
    created_at: ResponseDatetime
    category: AuditCategory
    actor: Optional[AuditActorResponse] = Field(description="Who acted; null for the system or an anonymous caller")
    resource_type: str
    resource_id: Optional[int]
    scope: Optional[AuditScopeResponse] = Field(description="Where the event happened; null for a system-wide event")
    success: bool
    old_values: Optional[Dict[str, Any]]
    new_values: Optional[Dict[str, Any]]
    request_id: Optional[str] = Field(description="X-Request-ID of the request that wrote the row")
    ip_address: Optional[str] = Field(description=_DEVICE)
    user_agent: Optional[str] = Field(description=_DEVICE)
    session_id: Optional[str] = Field(description=_DEVICE)
    request_url: Optional[str] = Field(description=_DEVICE)


def _camel(code: str) -> str:
    return "".join(part.capitalize() for part in code.replace(".", "_").split("_"))


def _params_model(event: AuditEvent) -> type:
    fields = {name: (annotation, None) if is_optional(annotation) else (annotation, ...)
              for name, annotation in event.params.items()}
    model = create_model(f"{_camel(event.code)}AuditParams", __base__=AuditParamsBase, **fields)
    model.__doc__ = f"Params of {event.code} ({event.title})"
    return model


def _entry_model(event: AuditEvent) -> type:
    model = create_model(
        f"{_camel(event.code)}AuditEntry", __base__=AuditEntryBase,
        event=(Literal[event.code], Field(description=_CODE_DOC)),  # type: ignore[valid-type]
        params=(_params_model(event), ...),
    )
    model.__doc__ = f"{event.title} (event {event.code}, category {event.category.value})"
    return model


@lru_cache(maxsize=None)
def audit_event_models() -> Dict[AuditEvent, type]:
    """One entry model per event of the catalog"""
    return {event: _entry_model(event) for event in AuditEvent}


AuditEntryResponse = Annotated[Union[tuple(audit_event_models().values())], Field(discriminator="event")]


class AuditEventResponse(AuditModel):
    """An event of the catalog (its params are typed in its <CamelCode>AuditParams schema)"""
    code: AuditEventCode = Field(description=_CODE_DOC)
    category: AuditCategory
    switchable: bool = Field(description="The category can be switched off (every category but audit)")
    title: str = Field(description="English fallback text")


def _served(entry: AuditEntry):
    """(event, params) the entry is served as: its own event when its params fit, legacy.unknown otherwise"""
    event = audit_event(entry.event) if entry.event else None
    if event is not None:
        params_model = audit_event_models()[event].model_fields["params"].annotation
        stored = dict(entry.params) if isinstance(entry.params, dict) else {}
        if event is AuditEvent.LEGACY_UNKNOWN:
            stored["stored_event"] = entry.event
        try:
            return event, params_model.model_validate(stored)
        except ValidationError:
            pass
    legacy = {"action": entry.action, "resource_type": entry.resource_type}
    if entry.message is not None:
        legacy["message"] = entry.message
    if entry.event is not None:
        legacy["stored_event"] = entry.event
    return AuditEvent.LEGACY_UNKNOWN, legacy


def audit_entry_to_response(entry: AuditEntry):
    """The response model of one entry (services/audit/query.py AuditEntry)"""
    event, params = _served(entry)
    category = entry.category if entry.category in {c.value for c in AuditCategory} else event.category.value
    return audit_event_models()[event].model_validate({
        "id": entry.id, "created_at": entry.created_at, "event": event.code, "category": category,
        "actor": None if entry.actor is None else dataclasses.asdict(entry.actor),
        "resource_type": entry.resource_type, "resource_id": entry.resource_id,
        "scope": None if entry.scope is None else dataclasses.asdict(entry.scope),
        "success": entry.success, "old_values": entry.old_values, "new_values": entry.new_values,
        "request_id": entry.request_id, "ip_address": entry.ip_address, "user_agent": entry.user_agent,
        "session_id": entry.session_id, "request_url": entry.request_url, "params": params,
    })


def audit_entries_to_response(entries: List[AuditEntry]) -> list:
    return [audit_entry_to_response(entry) for entry in entries]
