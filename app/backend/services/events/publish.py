"""publish(): the one writer of outbox_events. Last in the transaction, after its locks: then the events of one
resource come in order, and the id is taken as late as possible, which keeps late commits short. A plain INSERT with
a new key: it takes no lock anyone waits on, cannot answer 1020 under an old snapshot and has no place in any lock
chain (verified on MariaDB 11.8.9).

publish only adds a row to the session: it is synchronous, so any writer (a route, the media worker, a CLI tool) calls
it where its transaction is open, and the INSERT goes with the next flush, the commit. A route that flushes after
publish takes the id earlier than it needs: publish after the last flush, next to the audit row. Nothing is written
while events.enabled is false. A wrong call (an unknown, missing or mistyped param) is a TypeError: a programming
error found by the tests, as for the audit catalog.

Who receives it (the columns scope_key, permission, user_id of migration 037, unchanged):

    scope=<Node>, permission         the holders of the permission in effect at the node (AuthContext: has_at_post at
                                     a post, on_path at a group, has_at_root at the root)
    scope=HELD_ANYWHERE, permission  the holders of the permission at some node in effect (require(all=[X]) without
                                     at_root: AuthContext.has_anywhere): global objects, roles and dictionaries
    scope=EVERY_STREAM               every authenticated stream (reading needs only a login: the user guides)
    user_id                          also this user, whatever their rights; alone: this user only

HELD_ANYWHERE and EVERY_STREAM are stored as scope_key "*" (with and without a permission): "*" is never a node key
(Node.parse refuses it), so the node modes stay exact and no column changed. The renderer leaves scope out for it.
"""
import json
from typing import Any, Dict, Iterable, Literal, Optional, Union, get_args, get_origin

from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.auth.scope import ROOT, Node
from app.backend.config import settings
from app.backend.enums.events import PAYLOAD_LIMIT, Dictionary, EventType, MediaChange, ObservationChange
from app.backend.enums.general import Permissions
from app.backend.models.outbox_event import OutboxEvent

ANYWHERE_KEY = "*"       # scope_key of HELD_ANYWHERE and EVERY_STREAM: never a node key


class Audience:
    """A place that is no node of the tree: HELD_ANYWHERE (with a permission) or EVERY_STREAM (without one)"""
    __slots__ = ("name", "needs_permission")
    key = ANYWHERE_KEY

    def __init__(self, name: str, needs_permission: bool):
        self.name, self.needs_permission = name, needs_permission

    def __repr__(self) -> str:
        return self.name


HELD_ANYWHERE = Audience("HELD_ANYWHERE", needs_permission=True)
EVERY_STREAM = Audience("EVERY_STREAM", needs_permission=False)

# The view permission of each dictionary; None: every stream (reading the guides needs only a login)
DICTIONARY_VIEW: Dict[Dictionary, Optional[Permissions]] = {
    Dictionary.CATEGORIES: Permissions.CATEGORY_VIEW,
    Dictionary.SIGNAL_TYPES: Permissions.SIGNAL_TYPE_VIEW,
    Dictionary.TAGS: Permissions.TAG_VIEW,
    Dictionary.MEDIA_FORMATS: Permissions.MEDIA_FORMAT_VIEW,
    Dictionary.GUIDES: None,
}


def _check(event: EventType, params: dict) -> None:
    unknown, missing = set(params) - set(event.params), set(event.params) - set(params)
    if unknown or missing:
        raise TypeError(f"{event.value}: unknown params {sorted(unknown)}, missing {sorted(missing)}")
    for name, annotation in event.params.items():
        value = params[name]
        if get_origin(annotation) is Literal:
            if value not in get_args(annotation):
                raise TypeError(f"{event.value}: {name}={value!r} is not one of {get_args(annotation)}")
        elif annotation is int or (annotation == Optional[int] and value is not None):
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"{event.value}: {name} must be an int, not {type(value).__name__}")
        elif annotation == Optional[int]:
            pass                                    # None: absent from the data, never null
        else:
            raise TypeError(f"{event.value}: param {name} has a type the catalog does not allow")


def _published(session: AsyncSession, event: EventType) -> None:
    """Called by every valid publish(), events on or off; nothing to do here. tests/conftest.py (events_follow_audit)
    wraps it to check that each audited write of the administration publishes its event (AUDIT_EVENT_TYPES)."""


def publish(session: AsyncSession, event: EventType, *, scope: Union[Node, Audience, None] = None,
            permission: Optional[Permissions] = None, user_id: Optional[int] = None,
            conflate_key: Optional[str] = None, **params: Any) -> None:
    """Add the event to the transaction of the change. Delivered to the holders of permission at scope (the rules of
    a request: AuthContext; the module docstring has the modes), and to user_id whatever their rights. At least one of
    scope / user_id; a node and HELD_ANYWHERE need a permission, EVERY_STREAM takes none."""
    if not isinstance(event, EventType):
        raise TypeError(f"publish needs an EventType, not {event!r}")
    _check(event, params)
    if scope is None and user_id is None:
        raise TypeError(f"{event.value}: an event needs a scope or a user")
    if isinstance(scope, Audience):
        if scope.needs_permission != (permission is not None):
            raise TypeError(f"{event.value}: {scope!r} {'needs a' if scope.needs_permission else 'takes no'} permission")
    elif scope is not None and not isinstance(scope, Node):
        raise TypeError(f"{event.value}: a scope is a Node, HELD_ANYWHERE or EVERY_STREAM, not {scope!r}")
    elif (scope is None) != (permission is None):
        raise TypeError(f"{event.value}: a scope needs a permission, and a permission a scope")
    if permission is not None and not isinstance(permission, Permissions):
        raise TypeError(f"{event.value}: permission must be a Permissions member")
    if conflate_key is not None and len(conflate_key) > 48:
        raise TypeError(f"{event.value}: conflate_key is longer than its column")
    _published(session, event)
    if not settings.events.enabled:
        return
    payload = json.dumps({name: value for name, value in params.items() if value is not None}, separators=(",", ":"))
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


# ---------------------------------------------------------------- the administration: one event per operation

def publish_user_changed(session: AsyncSession, user_id: int, *homes: Node) -> None:
    """user.changed: to user.view in effect at the user's home node (AccessControlService.check_user_view: the home
    post, the root without one) and to the user. A move of the home passes the new and the old one: the second
    reaches those who just lost sight of the user (the user only on the first). Conflated per user."""
    seen = set()
    for home in homes:
        if home.key not in seen:
            publish(session, EventType.USER_CHANGED, scope=home, permission=Permissions.USER_VIEW,
                    user_id=None if seen else user_id, conflate_key=f"user:{user_id}", subject_user_id=user_id)
            seen.add(home.key)


def publish_certificate_changed(session: AsyncSession, user_id: int, home: Node, certificate_id: int) -> None:
    """certificate.changed: to certificate.view in effect at the user's home node and to the user; ids only"""
    publish(session, EventType.CERTIFICATE_CHANGED, scope=home, permission=Permissions.CERTIFICATE_VIEW,
            user_id=user_id, conflate_key=f"certificate:{certificate_id}", subject_user_id=user_id,
            certificate_id=certificate_id)


def publish_session_changed(session: AsyncSession, user_id: int, home: Node) -> None:
    """session.changed: to user.view in effect at the user's home node and to the user (their devices). Conflated per
    user: a burst of logins waits in a stream's queue as one event"""
    publish(session, EventType.SESSION_CHANGED, scope=home, permission=Permissions.USER_VIEW, user_id=user_id,
            conflate_key=f"session:{user_id}", subject_user_id=user_id)


def publish_role_changed(session: AsyncSession, role_id: int) -> None:
    """role.changed: to the holders of role.view at some node (roles are global objects). The hubs reload the grants
    of their connected users on it."""
    publish(session, EventType.ROLE_CHANGED, scope=HELD_ANYWHERE, permission=Permissions.ROLE_VIEW,
            conflate_key=f"role:{role_id}", role_id=role_id)


def publish_dictionary_changed(session: AsyncSession, dictionary: Dictionary) -> None:
    """dictionary.changed: to the holders of the dictionary's view permission at some node; the guides to every
    stream (the event names only the dictionary)"""
    permission = DICTIONARY_VIEW[dictionary]
    publish(session, EventType.DICTIONARY_CHANGED, scope=HELD_ANYWHERE if permission is not None else EVERY_STREAM,
            permission=permission, conflate_key=f"dictionary:{dictionary.value}", dictionary=dictionary.value)


# ---------------------------------------------------------------- the work screens: one event per operation

def publish_observation_changed(session: AsyncSession, observation_id: int, post_id: int,
                                change: ObservationChange) -> None:
    """observation.changed: to observation.view in effect at the observation's post (as GET /observations/{id}
    decides), conflated per observation. A band change and a disappearance are updated; an archive of the post or of a
    unit above it sends none per observation (its scope.changed tells the tree)."""
    publish(session, EventType.OBSERVATION_CHANGED, scope=Node.post(post_id), permission=Permissions.OBSERVATION_VIEW,
            conflate_key=f"observation:{observation_id}", observation_id=observation_id,
            observation_change=ObservationChange(change).value)


def publish_media_changed(session: AsyncSession, media_id: int, observation_id: Optional[int],
                          post_id: Optional[int], change: MediaChange) -> None:
    """media.changed: to media.view in effect at the post of the media's observation, conflated per media. Never per
    chunk or upload session: created once the file is stored (the transaction of its audit row). Media of old data
    without an observation (observation_id and post_id None): at the root, where its audit row goes, observation_id
    absent."""
    scope = Node.post(post_id) if post_id is not None else ROOT
    publish(session, EventType.MEDIA_CHANGED, scope=scope, permission=Permissions.MEDIA_VIEW,
            conflate_key=f"media:{media_id}", media_id=media_id, observation_id=observation_id,
            media_change=MediaChange(change).value)
