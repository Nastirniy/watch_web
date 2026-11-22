"""publish(): the one writer of outbox_events. Last in the transaction, after its locks: then the events of one
resource come in order, and the id is taken as late as possible, which keeps late commits short. A plain INSERT with
a new key: it takes no lock anyone waits on, cannot answer 1020 under an old snapshot and has no place in any lock
chain (verified on MariaDB 11.8.9).

publish only adds a row to the session: it is synchronous, so any writer (a route, the media worker, a CLI tool) calls
it where its transaction is open, and the INSERT goes with the next flush, the commit. A route that flushes after
publish takes the id earlier than it needs: publish after the last flush, next to the audit row. Nothing is written
while events.enabled is false. A wrong call (an unknown, missing or mistyped param) is a TypeError: a programming
error found by the tests, as for the audit catalog.
"""
import json
from typing import Any, Iterable, Literal, Optional, get_args, get_origin

from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.auth.scope import Node
from app.backend.config import settings
from app.backend.enums.events import PAYLOAD_LIMIT, EventType
from app.backend.enums.general import Permissions
from app.backend.models.outbox_event import OutboxEvent


def _check(event: EventType, params: dict) -> None:
    unknown, missing = set(params) - set(event.params), set(event.params) - set(params)
    if unknown or missing:
        raise TypeError(f"{event.value}: unknown params {sorted(unknown)}, missing {sorted(missing)}")
    for name, annotation in event.params.items():
        value = params[name]
        if get_origin(annotation) is Literal:
            if value not in get_args(annotation):
                raise TypeError(f"{event.value}: {name}={value!r} is not one of {get_args(annotation)}")
        elif annotation is int:
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"{event.value}: {name} must be an int, not {type(value).__name__}")
        else:
            raise TypeError(f"{event.value}: param {name} has a type the catalog does not allow")


def publish(session: AsyncSession, event: EventType, *, scope: Optional[Node] = None,
            permission: Optional[Permissions] = None, user_id: Optional[int] = None,
            conflate_key: Optional[str] = None, **params: Any) -> None:
    """Add the event to the transaction of the change. Delivered to the holders of permission in effect at scope (the
    rules of a request: AuthContext), and to user_id whatever their rights. At least one of scope / user_id; a scope
    needs a permission."""
    if not isinstance(event, EventType):
        raise TypeError(f"publish needs an EventType, not {event!r}")
    _check(event, params)
    if scope is None and user_id is None:
        raise TypeError(f"{event.value}: an event needs a scope or a user")
    if (scope is None) != (permission is None):
        raise TypeError(f"{event.value}: a scope needs a permission, and a permission a scope")
    if conflate_key is not None and len(conflate_key) > 48:
        raise TypeError(f"{event.value}: conflate_key is longer than its column")
    if not settings.events.enabled:
        return
    payload = json.dumps(params, separators=(",", ":"))
    if len(payload) > PAYLOAD_LIMIT:      # the catalog test keeps every event below it; never reached
        raise TypeError(f"{event.value}: payload longer than {PAYLOAD_LIMIT}")
    session.add(OutboxEvent(
        type=event.value, v=event.version, scope_key=scope.key if scope is not None else None,
        permission=permission.value if permission is not None else None, user_id=user_id,
        conflate_key=conflate_key, payload=payload))


def publish_scope_changed(session: AsyncSession, nodes: Iterable[Node]) -> None:
    """scope.changed once per distinct node, for post.view holders: a node that moved (to the new and the old parent:
    the second reaches those who just lost it), archived or restored. One per operation, never per row."""
    seen = set()
    for node in nodes:
        if node.key not in seen:
            seen.add(node.key)
            publish(session, EventType.SCOPE_CHANGED, scope=node, permission=Permissions.POST_VIEW)
