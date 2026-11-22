"""Messages of GET /events/stream in v1, and their renderer.

Every message is one text/event-stream block: `id:` the position (send it back as Last-Event-ID), `event:` the type,
`data:` one line of this JSON, `retry:` when the server sets it. StreamMessage is the union of every message,
discriminated by `type`: openapi-typescript gives an exact type, and a new event type is a compile error in an
exhaustive switch. A new EventType needs its member here (tests/test_event_stream_route.py).

V1Renderer builds the bytes from a row once per event (never per subscriber) without a pydantic round trip; a test
validates its output against StreamMessage.
"""
import json
from datetime import datetime
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, RootModel, model_serializer

from app.backend.auth.scope import Node
from app.backend.enums.events import MOVE_STATUSES, StreamCloseReason, StreamMessageType
from app.backend.errors.openapi import absent_not_null
from app.backend.utils.validators import ResponseDatetime

REFETCH = Field(description="Refetch what this event concerns after a random wait below this many milliseconds, never "
                            "at once: the server spreads the refetches of everyone who received it. A user's own action "
                            "is refetched at once by the page that made it")


class _Absent(BaseModel):
    """An optional member is absent, never null"""
    model_config = ConfigDict(json_schema_extra=lambda schema, cls: absent_not_null(schema))

    @model_serializer(mode="wrap")
    def _absent_not_null(self, handler):
        return {name: value for name, value in handler(self).items() if value is not None}


class EventScope(_Absent):
    """Where the event happened: the root, a scope group or a post"""
    kind: Literal["root", "group", "post"]
    id: int = Field(default=None, description="Id of the group or the post; absent at the root")


class EmptyData(BaseModel):
    """No data: the type says everything"""
    model_config = ConfigDict(extra="forbid")


class MoveChangedData(BaseModel):
    move_id: int = Field(description="Read GET /moves/{move_id}")
    move_status: MOVE_STATUSES


class CertificateMismatchData(BaseModel):
    audit_log_id: int = Field(description="Read GET /audit-logs/{audit_log_id}")
    session_user_id: int = Field(description="The user whose session's token came with another client certificate")


class CloseData(BaseModel):
    reason: StreamCloseReason = Field(description="token_expired: refresh (X-Activity: background) and reconnect; "
                                                  "session_ended: stop; restart: reconnect after the retry it carries")


class EventEnvelope(_Absent):
    """The fields of every domain event"""
    id: str = Field(description="Identity of the event, for ignoring a repeat (at-least-once delivery). Opaque: never "
                                "compare, compute or parse it; it may be lower than a position already seen")
    v: int = Field(description="Version of data for this type")
    occurred_at: ResponseDatetime
    scope: EventScope = Field(default=None, description="Where it happened; absent: an event for this user only")
    refetch_within_ms: int = REFETCH


class MoveChanged(EventEnvelope):
    """A move changed state (waiting, done, cancelled, failed): read GET /moves/{move_id}; when it is done the tree
    changed too (a scope.changed follows)"""
    type: Literal["move.changed"]
    data: MoveChangedData


class ScopeChanged(EventEnvelope):
    """The scope tree changed around this node (a move, an archive, a restore): reread GET /auth/me and the tree"""
    type: Literal["scope.changed"]
    data: EmptyData


class SecurityCertificateMismatch(EventEnvelope):
    """A valid token of a session was presented with another client certificate: the request was refused, the session
    stays, and an audit row session.certificate_mismatch records it (at most one per session in 5 minutes, and this
    event exactly as often). Sent to the holders of audit.view only, never to the user of the session"""
    type: Literal["security.certificate_mismatch"]
    data: CertificateMismatchData


class StreamReady(BaseModel):
    """The first message of a connection"""
    type: Literal["ready"]


class StreamHeartbeat(BaseModel):
    """About every 20 s. It carries the position too: store its id"""
    type: Literal["heartbeat"]


class StreamReload(BaseModel):
    """Too far behind, a position this server cannot answer for, or from before a restore: refetch what is on screen"""
    type: Literal["reload"]
    refetch_within_ms: int = REFETCH


class StreamClose(BaseModel):
    """The server ends the stream; the connection then ends"""
    type: Literal["close"]
    data: CloseData


class StreamMessage(RootModel[Annotated[Union[MoveChanged, ScopeChanged, SecurityCertificateMismatch, StreamReady,
                                              StreamHeartbeat, StreamReload, StreamClose],
                                        Field(discriminator="type")]]):
    """One message of the stream: event: is its type, data: this JSON"""


def _json(value: dict) -> bytes:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True).encode()


def _message(kind: str, value: dict) -> bytes:
    return b"event: " + kind.encode() + b"\ndata: " + _json(value) + b"\n\n"


def _occurred(value: datetime) -> str:
    """The database's UTC clock (naive) as ResponseDatetime writes it"""
    return value.strftime("%Y-%m-%dT%H:%M:%S.%fZ") if value.microsecond else value.strftime("%Y-%m-%dT%H:%M:%SZ")


def _scope(scope_key) -> dict:
    node = Node.parse(scope_key)
    return {"kind": node.kind} if node.id is None else {"kind": node.kind, "id": node.id}


class V1Renderer:
    """The messages of v1 as bytes from "event:" to the empty line (services/events/hub.py Renderer)"""

    def event(self, row, refetch_within_ms: int) -> bytes:
        value = {"id": str(row.id), "type": row.type, "v": row.v, "occurred_at": _occurred(row.occurred_at)}
        if row.scope_key is not None:
            value["scope"] = _scope(row.scope_key)
        value["data"] = json.loads(row.payload)
        value["refetch_within_ms"] = refetch_within_ms
        return _message(row.type, value)

    def ready(self) -> bytes:
        return _message(StreamMessageType.READY.value, {"type": StreamMessageType.READY.value})

    def heartbeat(self) -> bytes:
        return _message(StreamMessageType.HEARTBEAT.value, {"type": StreamMessageType.HEARTBEAT.value})

    def reload(self, refetch_within_ms: int) -> bytes:
        return _message(StreamMessageType.RELOAD.value, {"type": StreamMessageType.RELOAD.value,
                                                         "refetch_within_ms": refetch_within_ms})

    def close(self, reason: StreamCloseReason) -> bytes:
        return _message(StreamMessageType.CLOSE.value, {"type": StreamMessageType.CLOSE.value,
                                                        "data": {"reason": reason.value}})


render_v1 = V1Renderer()
