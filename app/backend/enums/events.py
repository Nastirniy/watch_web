"""Events to the web client: the catalog of what the outbox may carry.

A code is the key of exactly one kind of event. Params are ids and values of an enum, never free text: an event
cannot leak content because it has no field for it. An Optional[int] param may be None: it is then absent from the
data, never null. The web refetches through REST (API.md, "Real-time events").
A new type goes here, into the union of api/v1/models/response/events.py and into the web's switch.
"""
from enum import Enum
from typing import Any, Dict, Literal, NamedTuple, Optional

from app.backend.enums.audit import AuditCategory, AuditEvent

PAYLOAD_LIMIT = 512          # outbox_events.payload: tests/events/test_event_catalog.py keeps every event below it
MOVE_STATUSES = Literal["waiting", "done", "cancelled", "failed"]   # MoveStatus (tests/events/test_event_catalog.py)


class Dictionary(str, Enum):
    """The dictionaries dictionary.changed names: the list pages of the administration and the pickers"""
    CATEGORIES = "categories"
    SIGNAL_TYPES = "signal_types"
    TAGS = "tags"
    MEDIA_FORMATS = "media_formats"
    GUIDES = "guides"


DICTIONARIES = Literal["categories", "signal_types", "tags", "media_formats", "guides"]   # Dictionary (catalog test)


class ObservationChange(str, Enum):
    """What happened to the observation observation.changed names (a band change, a disappearance: updated)"""
    CREATED = "created"
    UPDATED = "updated"
    ARCHIVED = "archived"
    RESTORED = "restored"


OBSERVATION_CHANGES = Literal["created", "updated", "archived", "restored"]   # ObservationChange (catalog test)


class MediaChange(str, Enum):
    """What happened to the media media.changed names. processed: the media worker ended its poster or storyboard
    in a state a page shows (ready, failed, unsupported): thumbnail_status or storyboard_status changed"""
    CREATED = "created"
    ARCHIVED = "archived"
    RESTORED = "restored"
    PROCESSED = "processed"


MEDIA_CHANGES = Literal["created", "archived", "restored", "processed"]   # MediaChange (catalog test)


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
    # The administration (owner, 2026-11-22). subject_user_id, never user_id: publish() targets a user by that name.
    # A user record, its activation flags, type or role assignments changed: to user.view over the user and the user
    USER_CHANGED = ("user.changed", EventSpec(1, {"subject_user_id": int}))
    # A certificate of the user was issued, uploaded or revoked: to certificate.view over the user and the user
    CERTIFICATE_CHANGED = ("certificate.changed", EventSpec(1, {"subject_user_id": int, "certificate_id": int}))
    # A login, a logout, a session ended by the server: to user.view over the user and the user (conflated per user)
    SESSION_CHANGED = ("session.changed", EventSpec(1, {"subject_user_id": int}))
    # A role, its permissions, its archive or its permanent delete: to the holders of role.view anywhere
    ROLE_CHANGED = ("role.changed", EventSpec(1, {"role_id": int}))
    # Any change of a dictionary: to the holders of its view permission anywhere (guides: every stream)
    DICTIONARY_CHANGED = ("dictionary.changed", EventSpec(1, {"dictionary": DICTIONARIES}))
    # The work screens (owner, 2026-11-23): at the post of the observation, to the holders of the view permission in
    # effect there. Conflated per observation / per media.
    # An observation created, updated (any field, its tags, a band, a disappearance), archived or restored
    OBSERVATION_CHANGED = ("observation.changed", EventSpec(1, {"observation_id": int,
                                                                "observation_change": OBSERVATION_CHANGES}))
    # A media file of an observation stored (any upload transport), archived, restored, or its poster / storyboard
    # processed; media without an observation (old data) publishes none
    MEDIA_CHANGED = ("media.changed", EventSpec(1, {"media_id": int, "observation_id": int,
                                                    "media_change": MEDIA_CHANGES}))

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
    RIGHTS_CHANGED = "rights_changed"   # what is in effect for this user changed: reread GET /auth/me
    CLOSE = "close"


# The event each audited write publishes in the transaction of its audit row (None: no event, with the reason).
# tests/events/test_event_admin.py and tests/events/test_event_data.py keep every event of the categories authentication, access,
# dictionaries and data in this map, and tests/conftest.py (events_follow_audit) fails any committed transaction whose audit
# row of a mapped event comes without a publish() of its type: a new audited write path cannot forget its event.
AUDIT_EVENT_TYPES: Dict[AuditEvent, Optional[EventType]] = {
    # authentication
    AuditEvent.USER_LOGIN: EventType.SESSION_CHANGED,
    AuditEvent.AUTH_CERTIFICATE_LOGIN: None,        # an attempt: a success goes on to user.login, which publishes
    AuditEvent.SESSION_LOGOUT: EventType.SESSION_CHANGED,
    AuditEvent.SESSION_CERTIFICATE_MISMATCH: EventType.SECURITY_CERTIFICATE_MISMATCH,
    # access
    AuditEvent.SESSION_END: EventType.SESSION_CHANGED,
    AuditEvent.USER_CREATE: EventType.USER_CHANGED,
    AuditEvent.USER_UPDATE: EventType.USER_CHANGED,
    AuditEvent.USER_ARCHIVE: EventType.USER_CHANGED,
    AuditEvent.USER_RESTORE: EventType.USER_CHANGED,
    AuditEvent.ROLE_ASSIGNMENT_CREATE: EventType.USER_CHANGED,
    AuditEvent.ROLE_ASSIGNMENT_DELETE: EventType.USER_CHANGED,
    AuditEvent.SYSTEM_ROLE_ASSIGNMENT_CREATE: EventType.USER_CHANGED,
    AuditEvent.SYSTEM_ROLE_ASSIGNMENT_DELETE: EventType.USER_CHANGED,
    AuditEvent.USER_CERTIFICATE_ISSUE: EventType.CERTIFICATE_CHANGED,
    AuditEvent.USER_CERTIFICATE_UPLOAD: EventType.CERTIFICATE_CHANGED,
    AuditEvent.USER_CERTIFICATE_EXPORT: None,       # a read of the package: the certificate stays as it was
    AuditEvent.USER_CERTIFICATE_REVOKE: EventType.CERTIFICATE_CHANGED,
    AuditEvent.ROLE_CREATE: EventType.ROLE_CHANGED,
    AuditEvent.ROLE_UPDATE: EventType.ROLE_CHANGED,
    AuditEvent.ROLE_ARCHIVE: EventType.ROLE_CHANGED,
    AuditEvent.ROLE_RESTORE: EventType.ROLE_CHANGED,
    AuditEvent.ROLE_CLONE: EventType.ROLE_CHANGED,
    AuditEvent.ROLE_DELETE: EventType.ROLE_CHANGED,
    AuditEvent.ROLE_PERMISSION_ADD: EventType.ROLE_CHANGED,
    AuditEvent.ROLE_PERMISSION_REMOVE: EventType.ROLE_CHANGED,
    # dictionaries
    **{event: EventType.DICTIONARY_CHANGED for event in AuditEvent
       if event.category is AuditCategory.DICTIONARIES},
    # data
    AuditEvent.OBSERVATION_CREATE: EventType.OBSERVATION_CHANGED,
    AuditEvent.OBSERVATION_UPDATE: EventType.OBSERVATION_CHANGED,
    AuditEvent.OBSERVATION_DISAPPEAR: EventType.OBSERVATION_CHANGED,
    AuditEvent.OBSERVATION_ARCHIVE: EventType.OBSERVATION_CHANGED,
    AuditEvent.OBSERVATION_RESTORE: EventType.OBSERVATION_CHANGED,
    AuditEvent.OBSERVATION_FREQUENCY_CREATE: EventType.OBSERVATION_CHANGED,
    AuditEvent.OBSERVATION_FREQUENCY_UPDATE: EventType.OBSERVATION_CHANGED,
    AuditEvent.OBSERVATION_FREQUENCY_ARCHIVE: EventType.OBSERVATION_CHANGED,
    AuditEvent.OBSERVATION_FREQUENCY_RESTORE: EventType.OBSERVATION_CHANGED,
    AuditEvent.MEDIA_CREATE: EventType.MEDIA_CHANGED,
    AuditEvent.MEDIA_ARCHIVE: EventType.MEDIA_CHANGED,
    AuditEvent.MEDIA_RESTORE: EventType.MEDIA_CHANGED,
    AuditEvent.MEDIA_SHARE: None,                   # a signed link handed out: the media stays as it was
    AuditEvent.UPLOAD_SESSION_CREATE: None,         # an upload in progress: no media yet, the completion publishes
    AuditEvent.UPLOAD_SESSION_ABORT: None,          # no media made; an archive that cancels it publishes its own
}
