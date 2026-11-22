"""Events to the web client: the catalog of what the outbox may carry.

A code is the key of exactly one kind of event. Params are ids and values of an enum, never free text: an event
cannot leak content because it has no field for it. The web refetches through REST (API.md, "Real-time events").
A new type goes here, into the union of api/v1/models/response/events.py and into the web's switch.
"""
from enum import Enum
from typing import Any, Dict, Literal, NamedTuple

PAYLOAD_LIMIT = 512          # outbox_events.payload: tests/test_event_catalog.py keeps every event below it
MOVE_STATUSES = Literal["waiting", "done", "cancelled", "failed"]   # MoveStatus (tests/test_event_catalog.py)


class EventSpec(NamedTuple):
    version: int
    params: Dict[str, Any]


class EventType(str, Enum):
    MOVE_CHANGED = ("move.changed", EventSpec(1, {"move_id": int, "move_status": MOVE_STATUSES}))
    SCOPE_CHANGED = ("scope.changed", EventSpec(1, {}))
    # A valid token of a session came with another client certificate (the audit row session.certificate_mismatch):
    # to the holders of audit.view, as often as that row is written. Ids only, never a token or a certificate value
    # (session_user_id, not user_id: publish() targets a user by that name, and this event never goes to that user)
    SECURITY_CERTIFICATE_MISMATCH = ("security.certificate_mismatch", EventSpec(1, {"audit_log_id": int,
                                                                                   "session_user_id": int}))

    def __new__(cls, code: str, spec: EventSpec):
        member = str.__new__(cls, code)
        member._value_ = code
        member.version, member.params = spec.version, spec.params
        return member


class StreamCloseReason(str, Enum):
    """Why the server ends a stream (the close message)"""
    TOKEN_EXPIRED = "token_expired"     # refresh, then reconnect
    SESSION_ENDED = "session_ended"     # stop: the session is over
    RESTART = "restart"                 # the server stops: reconnect after the retry it carries


class StreamMessageType(str, Enum):
    """Messages of the stream itself, next to the domain events of EventType"""
    READY = "ready"
    HEARTBEAT = "heartbeat"
    RELOAD = "reload"
    CLOSE = "close"
