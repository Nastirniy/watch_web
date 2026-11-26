"""Events of the administration (owner, 2026-11-22): user.changed, certificate.changed, session.changed, role.changed,
dictionary.changed. One event per operation, in the transaction of the change after its audit row; ids and enum
values only; who receives them (the holders in effect, the subject user, every stream for the guides); a refused write
publishes nothing; the hub reloads the grants a user.changed or role.changed concerns before it delivers."""
import json

import pytest
from cryptography.hazmat.primitives import serialization
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.auth.context import load_auth_context
from app.backend.auth.scope import Node, ROOT
from app.backend.auth.service import AuthService
from app.backend.config import EventsConfig, settings
from app.backend.enums.audit import AuditCategory, AuditEvent
from app.backend.enums.events import AUDIT_EVENT_TYPES, Dictionary, EventType
from app.backend.enums.general import BuiltinRole, Permissions as P, SessionRevokeCause, UserType
from app.backend.enums.routes import (AuthRoutes, CategoryRoutes, GuideRoutes, MediaFormatRoutes, PostRoutes,
                                      RoleRoutes, SignalTypeRoutes, TagRoutes, UserRoutes)
from app.backend.models.outbox_event import OutboxEvent
from app.backend.models.role import Permission
from app.backend.services.events import EVERY_STREAM, HELD_ANYWHERE, publish
from app.backend.services.events.hub import EventHub
from tests.helpers import (assign, builtin_role, give, issue, make_admin, make_certificate, make_group, make_post,
                           make_role, make_user)
from tests.events.test_event_hub import TextRenderer, later, queued

ADMIN_TYPES = {EventType.USER_CHANGED, EventType.CERTIFICATE_CHANGED, EventType.SESSION_CHANGED,
               EventType.ROLE_CHANGED, EventType.DICTIONARY_CHANGED}


@pytest.fixture
def events_on(monkeypatch):
    monkeypatch.setattr(settings.events, "enabled", True)


async def _rows(db, *types: EventType) -> list:
    query = select(OutboxEvent).order_by(OutboxEvent.id).execution_options(populate_existing=True)
    if types:
        query = query.where(OutboxEvent.type.in_([t.value for t in types]))
    return list((await db.exec(query)).all())


def _summary(rows) -> list:
    return [(row.type, row.scope_key, row.permission, row.user_id, json.loads(row.payload)) for row in rows]


def _user_changed(user_id: int, home: str, to_user: bool = True) -> tuple:
    return ("user.changed", home, "user.view", user_id if to_user else None, {"subject_user_id": user_id})


def _session_changed(user_id: int, home: str) -> tuple:
    return ("session.changed", home, "user.view", user_id, {"subject_user_id": user_id})


def _pem(common_name: str, serial: int) -> bytes:
    return make_certificate(common_name, serial).public_bytes(serialization.Encoding.PEM)


# ---------------------------------------------------------------- the catalog and its map

def test_every_audited_event_of_the_administration_is_mapped():
    administration = {AuditCategory.AUTHENTICATION, AuditCategory.ACCESS, AuditCategory.DICTIONARIES}
    mapped = {event for event in AUDIT_EVENT_TYPES if event.category in administration}
    assert {event for event in AuditEvent if event.category in administration} == mapped
    assert {AUDIT_EVENT_TYPES[event] for event in mapped} - {None} == ADMIN_TYPES | {
        EventType.SECURITY_CERTIFICATE_MISMATCH}
    # the work screens (data) are mapped too: tests/events/test_event_data.py; nothing else
    assert {event.category for event in AUDIT_EVENT_TYPES} == administration | {AuditCategory.DATA}


@pytest.mark.parametrize("event", sorted(ADMIN_TYPES, key=lambda e: e.value))
def test_params_are_ids_and_enum_values_only(event):
    for name, annotation in event.params.items():
        assert name != "user_id", "publish() targets a user by that name"
        assert annotation is int or name == "dictionary"


def test_dictionaries_are_the_enum():
    from typing import get_args
    from app.backend.enums.events import DICTIONARIES
    assert set(get_args(DICTIONARIES)) == {d.value for d in Dictionary}


# ---------------------------------------------------------------- publish: the two places that are no node

async def test_held_anywhere_and_every_stream_are_stored_as_star(db, events_on):
    publish(db, EventType.ROLE_CHANGED, scope=HELD_ANYWHERE, permission=P.ROLE_VIEW, role_id=4)
    publish(db, EventType.DICTIONARY_CHANGED, scope=EVERY_STREAM, dictionary="guides")
    await db.commit()
    assert _summary(await _rows(db)) == [("role.changed", "*", "role.view", None, {"role_id": 4}),
                                         ("dictionary.changed", "*", None, None, {"dictionary": "guides"})]


async def test_the_places_take_the_permission_they_need(db, events_on):
    with pytest.raises(TypeError):
        publish(db, EventType.ROLE_CHANGED, scope=HELD_ANYWHERE, role_id=4)
    with pytest.raises(TypeError):
        publish(db, EventType.DICTIONARY_CHANGED, scope=EVERY_STREAM, permission=P.TAG_VIEW, dictionary="tags")
    with pytest.raises(TypeError):
        publish(db, EventType.DICTIONARY_CHANGED, scope="*", permission=P.TAG_VIEW, dictionary="tags")
    with pytest.raises(TypeError):
        publish(db, EventType.DICTIONARY_CHANGED, scope=HELD_ANYWHERE, permission=P.TAG_VIEW, dictionary="other")
    with pytest.raises(TypeError):
        publish(db, EventType.USER_CHANGED, scope=ROOT, permission="user.view", subject_user_id=1)
    await db.commit()
    assert await _rows(db) == []


# ---------------------------------------------------------------- user.changed

async def test_user_writes_publish_one_user_changed_each(client, db, headers, events_on):
    post, other = await make_post(db), await make_post(db)
    admin = await make_admin(db)
    h = headers(admin)
    created = await client.post(V1.path(UserRoutes.ROOT), headers=h, data={
        "username": "evented", "username_alias": "Evented", "user_type": "operator", "post_id": str(post.id)})
    assert created.status_code == 200, created.text
    user_id = created.json()["id"]
    renamed = await client.patch(V1.path(UserRoutes.USER_BY_ID, user_id=user_id), headers=h,
                                 data={"username_alias": "Renamed"})
    assert renamed.status_code == 200, renamed.text
    moved = await client.patch(V1.path(UserRoutes.USER_BY_ID, user_id=user_id), headers=h,
                               data={"post_id": str(other.id)})
    assert moved.status_code == 200, moved.text
    archived = await client.post(V1.path(UserRoutes.USER_ARCHIVE, user_id=user_id), headers=h)
    assert archived.status_code == 200, archived.text
    restored = await client.post(V1.path(UserRoutes.USER_RESTORE, user_id=user_id), headers=h)
    assert restored.status_code == 200, restored.text
    assert _summary(await _rows(db)) == [
        _user_changed(user_id, f"p:{post.id}"),                                 # create
        _user_changed(user_id, f"p:{post.id}"),                                 # rename
        _user_changed(user_id, f"p:{other.id}"), _user_changed(user_id, f"p:{post.id}", to_user=False),  # move
        _user_changed(user_id, f"p:{other.id}"),                                # archive (no live session)
        _user_changed(user_id, f"p:{other.id}"),                                # restore
    ]


async def test_a_deactivation_ending_sessions_publishes_the_session_too(client, db, headers, events_on):
    post = await make_post(db)
    admin = await make_admin(db)
    target = await make_user(db, post_id=post.id)
    await AuthService.create_session(db, target)
    before = len(await _rows(db))
    answer = await client.patch(V1.path(UserRoutes.USER_BY_ID, user_id=target.id), headers=headers(admin),
                                data={"is_active": "false"})
    assert answer.status_code == 200, answer.text
    assert _summary((await _rows(db))[before:]) == [_user_changed(target.id, f"p:{post.id}"),
                                                    _session_changed(target.id, f"p:{post.id}")]


async def test_assignments_publish_user_changed_at_the_home_not_at_the_node(client, db, headers, events_on):
    post, other = await make_post(db), await make_post(db, "Other")
    admin = await make_admin(db)
    target = await make_user(db, post_id=post.id, user_type=UserType.OPERATOR)
    operator = await builtin_role(db, BuiltinRole.OPERATOR)
    await assign(db, target, operator, Node.post(post.id))
    editor = await builtin_role(db, BuiltinRole.TAG_EDITOR)
    h = headers(admin)
    created = await client.post(V1.path(UserRoutes.USER_ASSIGNMENTS, user_id=target.id), headers=h,
                                json={"role_id": operator.id, "scope": {"kind": "post", "id": other.id}})
    assert created.status_code == 201, created.text
    deleted = await client.delete(V1.path(UserRoutes.USER_ASSIGNMENT_BY_ID, user_id=target.id,
                                          assignment_id=created.json()["id"]), headers=h)
    assert deleted.status_code == 204, deleted.text
    system = await client.post(V1.path(UserRoutes.USER_SYSTEM_ASSIGNMENTS, user_id=target.id), headers=h,
                               json={"role_id": editor.id})
    assert system.status_code == 201, system.text
    taken = await client.delete(V1.path(UserRoutes.USER_SYSTEM_ASSIGNMENT_BY_ID, user_id=target.id,
                                        assignment_id=system.json()["id"]), headers=h)
    assert taken.status_code == 204, taken.text
    assert _summary(await _rows(db)) == [_user_changed(target.id, f"p:{post.id}")] * 4


async def test_refused_user_writes_publish_nothing(client, db, headers, events_on):
    post = await make_post(db)
    admin = await make_admin(db)
    clerk = await make_user(db, [P.USER_VIEW], post_id=post.id)
    target = await make_user(db, post_id=post.id)
    forbidden = await client.patch(V1.path(UserRoutes.USER_BY_ID, user_id=target.id), headers=headers(clerk),
                                   data={"username_alias": "Nope"})
    assert forbidden.status_code == 403, forbidden.text
    taken = await client.post(V1.path(UserRoutes.ROOT), headers=headers(admin), data={
        "username": target.username, "username_alias": "Again", "user_type": "viewer"})
    assert taken.status_code == 400, taken.text
    self_archive = await client.post(V1.path(UserRoutes.USER_ARCHIVE, user_id=admin.id), headers=headers(admin))
    assert self_archive.status_code == 400, self_archive.text
    not_archived = await client.post(V1.path(UserRoutes.USER_RESTORE, user_id=target.id), headers=headers(admin))
    assert not_archived.status_code == 409, not_archived.text
    assert await _rows(db) == []


async def test_events_off_publish_nothing_but_the_writes_still_work(client, db, headers):
    assert settings.events.enabled is False
    post = await make_post(db)
    admin = await make_admin(db)
    created = await client.post(V1.path(UserRoutes.ROOT), headers=headers(admin), data={
        "username": "quiet", "username_alias": "Quiet", "user_type": "viewer", "post_id": str(post.id)})
    assert created.status_code == 200, created.text
    tag = await client.post(V1.path(TagRoutes.CREATE), headers=headers(admin), json={"name": "quiet"})
    assert tag.status_code in (200, 201), tag.text
    assert await _rows(db) == []


# ---------------------------------------------------------------- session.changed

async def test_login_logout_and_server_ends_publish_session_changed(client, db, headers, events_on):
    post = await make_post(db)
    admin = await make_admin(db)
    user = await make_user(db, post_id=post.id)
    first = await AuthService.create_session(db, user)                         # the service the login route calls
    bearer = {"Authorization": f"Bearer {AuthService.session_tokens(user.id, first)[0]}"}
    assert (await client.post(V1.path(AuthRoutes.LOGOUT), headers=bearer)).status_code == 200
    second = await AuthService.create_session(db, user)
    ended = await client.post(V1.path(UserRoutes.USER_SESSION_END, user_id=user.id, session_id=second.id),
                              headers=headers(admin))
    assert ended.status_code == 200, ended.text
    await AuthService.create_session(db, user)
    all_ended = await client.post(V1.path(UserRoutes.USER_SESSIONS_END, user_id=user.id), headers=headers(admin))
    assert all_ended.status_code == 200 and all_ended.json()["ended_count"] == 1
    third = await AuthService.create_session(db, user)
    assert await AuthService.revoke_session(db, third, user, SessionRevokeCause.REFRESH_TOKEN_REUSED)
    rows = await _rows(db)
    assert _summary(rows) == [_session_changed(user.id, f"p:{post.id}")] * 8
    assert {row.conflate_key for row in rows} == {f"session:{user.id}"}


async def test_nothing_to_end_publishes_nothing(client, db, headers, events_on):
    admin = await make_admin(db)
    user = await make_user(db)
    none_live = await client.post(V1.path(UserRoutes.USER_SESSIONS_END, user_id=user.id), headers=headers(admin))
    assert none_live.status_code == 200 and none_live.json()["ended_count"] == 0
    assert await AuthService.invalidate_all_user_sessions(db, user.id, SessionRevokeCause.ENDED_BY_ADMIN) == 0
    assert await _rows(db) == []


async def test_a_burst_of_logins_waits_in_a_queue_as_one_event(engine, db, events_on):
    user = await make_user(db, user_type=UserType.ADMINISTRATOR)
    await give(db, user, [P.USER_VIEW])
    hub = EventHub(engine, TextRenderer(), EventsConfig(enabled=True), workers=1)
    await hub.start(run_tasks=False)
    watcher = hub.attach(await load_auth_context(db, user, session_id="w"), later(), None)
    subject = await make_user(db)
    for _ in range(5):
        await AuthService.create_session(db, subject)
    assert await hub.tick() == 5
    assert [event for _, _, event, _ in queued(watcher)] == ["ready", "session.changed"]


# ---------------------------------------------------------------- certificate.changed

async def test_certificate_writes_publish_ids_only(client, db, headers, pki, events_on):
    post = await make_post(db)
    admin = await make_admin(db)
    user = await make_user(db, post_id=post.id)
    issued = await issue(client, headers, admin, user)
    assert issued.status_code == 201, issued.text
    issued_id = issued.json()["certificate"]["id"]
    uploaded = await client.post(V1.path(UserRoutes.USER_CERTIFICATE_UPLOAD, user_id=user.id), headers=headers(admin),
                                 files={"file": ("cert.pem", _pem("cn-event-upload", 0x7A7A), "application/x-pem-file")},
                                 data={"label": "Office PC"})
    assert uploaded.status_code == 201, uploaded.text
    exported = await client.post(V1.path(UserRoutes.USER_CERTIFICATE_EXPORT, user_id=user.id,
                                         certificate_id=issued_id), headers=headers(admin))
    assert exported.status_code == 200, exported.text
    revoked = await client.post(V1.path(UserRoutes.USER_CERTIFICATE_REVOKE, user_id=user.id,
                                        certificate_id=issued_id), json={"reason": "key_compromise"},
                                headers=headers(admin))
    assert revoked.status_code == 200, revoked.text
    again = await client.post(V1.path(UserRoutes.USER_CERTIFICATE_REVOKE, user_id=user.id,
                                      certificate_id=issued_id), headers=headers(admin))
    assert again.status_code == 409, again.text
    rows = await _rows(db)
    changed = ("certificate.changed", f"p:{post.id}", "certificate.view", user.id)
    assert _summary(rows) == [
        (*changed, {"subject_user_id": user.id, "certificate_id": issued_id}),
        (*changed, {"subject_user_id": user.id, "certificate_id": uploaded.json()["id"]}),
        (*changed, {"subject_user_id": user.id, "certificate_id": issued_id}),     # export publishes nothing
    ]
    for row in rows:
        assert set(json.loads(row.payload)) == {"subject_user_id", "certificate_id"}
        assert issued.json()["certificate"]["serial"] not in row.payload


# ---------------------------------------------------------------- role.changed

async def test_role_writes_publish_role_changed_to_role_view_anywhere(client, db, headers, events_on):
    admin = await make_admin(db)
    h = headers(admin)
    created = await client.post(V1.path(RoleRoutes.ROOT), headers=h, json={
        "name": "event_watch", "display_name": "Event watch", "kind": "tree"})
    assert created.status_code == 201, created.text
    role_id = created.json()["id"]
    url = V1.path(RoleRoutes.ROLE_BY_ID, role_id=role_id)
    assert (await client.patch(url, headers=h, json={"description": "note"})).status_code == 200
    permission = (await db.exec(select(Permission).where(Permission.name == "tag.view"))).one()
    perm_url = V1.path(RoleRoutes.ROLE_PERMISSION_BY_ID, role_id=role_id, permission_id=permission.id)
    assert (await client.put(perm_url, headers=h)).status_code == 200
    assert (await client.delete(perm_url, headers=h)).status_code == 200
    clone = await client.post(V1.path(RoleRoutes.ROLE_CLONE, role_id=role_id), headers=h,
                              json={"name": "event_copy", "display_name": "Event copy"})
    assert clone.status_code == 201, clone.text
    for route in (RoleRoutes.ROLE_ARCHIVE, RoleRoutes.ROLE_RESTORE, RoleRoutes.ROLE_ARCHIVE):
        assert (await client.post(V1.path(route, role_id=role_id), headers=h)).status_code == 200
    assert (await client.delete(url, headers=h)).status_code == 204
    changed = lambda rid: ("role.changed", "*", "role.view", None, {"role_id": rid})   # noqa: E731
    assert _summary(await _rows(db)) == [changed(role_id)] * 4 + [changed(clone.json()["id"])] + [changed(role_id)] * 4


async def test_refused_role_writes_publish_nothing(client, db, headers, events_on):
    admin = await make_admin(db)
    viewer = await make_user(db, user_type=UserType.ADMINISTRATOR)
    await give(db, viewer, [P.ROLE_VIEW])
    role = await make_role(db, [P.TAG_VIEW])
    forbidden = await client.patch(V1.path(RoleRoutes.ROLE_BY_ID, role_id=role.id), headers=headers(viewer),
                                   json={"description": "nope"})
    assert forbidden.status_code == 403, forbidden.text
    live = await client.delete(V1.path(RoleRoutes.ROLE_BY_ID, role_id=role.id), headers=headers(admin))
    assert live.status_code == 409, live.text                    # archive first
    taken = await client.post(V1.path(RoleRoutes.ROOT), headers=headers(admin),
                              json={"name": role.name, "display_name": "Again", "kind": "tree"})
    assert taken.status_code in (400, 409), taken.text
    assert await _rows(db) == []


# ---------------------------------------------------------------- dictionary.changed

@pytest.mark.parametrize("dictionary, path, body, permission", [
    (Dictionary.CATEGORIES, V1.path(CategoryRoutes.CREATE), {"name": "Radar", "alias": "radar"}, "category.view"),
    (Dictionary.SIGNAL_TYPES, V1.path(SignalTypeRoutes.CREATE), {"code": "LTE", "name": "LTE uplink"},
     "signal_type.view"),
    (Dictionary.TAGS, V1.path(TagRoutes.CREATE), {"name": "urgent"}, "tag.view"),
    (Dictionary.MEDIA_FORMATS, V1.path(MediaFormatRoutes.CREATE),
     {"media_type": "video", "content_type": "video/x-event", "label": "Test", "extensions": [".evt"],
      "max_file_size_bytes": 1000}, "media_format.view"),
    (Dictionary.GUIDES, V1.path(GuideRoutes.ROOT), {"title": "Getting started"}, None),
])
async def test_a_dictionary_create_publishes_its_dictionary(client, db, headers, events_on, dictionary, path, body,
                                                            permission):
    created = await client.post(path, headers=headers(await make_admin(db)), json=body)
    assert created.status_code in (200, 201), created.text
    assert _summary(await _rows(db)) == [("dictionary.changed", "*", permission, None,
                                          {"dictionary": dictionary.value})]


async def test_a_refused_dictionary_write_publishes_nothing(client, db, headers, events_on):
    admin = await make_admin(db)
    assert (await client.post(V1.path(TagRoutes.CREATE), headers=headers(admin), json={"name": "dup"})).status_code \
        in (200, 201)
    before = len(await _rows(db))
    again = await client.post(V1.path(TagRoutes.CREATE), headers=headers(admin), json={"name": "dup"})
    assert again.status_code in (400, 409), again.text
    operator = await make_user(db, [P.TAG_VIEW])
    forbidden = await client.post(V1.path(TagRoutes.CREATE), headers=headers(operator), json={"name": "mine"})
    assert forbidden.status_code == 403, forbidden.text
    assert len(await _rows(db)) == before


# ---------------------------------------------------------------- who receives them

async def _hub(engine) -> EventHub:
    hub = EventHub(engine, TextRenderer(), EventsConfig(enabled=True), workers=1)
    await hub.start(run_tasks=False)
    return hub


def _received(connection) -> list:
    return [(event, data) for _, _, event, data in queued(connection)[1:]]


async def _attach(hub, db, user):
    return hub.attach(await load_auth_context(db, user, session_id=f"s{user.id}"), later(), None)


async def test_user_and_certificate_events_reach_the_holders_over_the_user_and_the_user(client, db, engine, headers,
                                                                                       pki, events_on):
    group = await make_group(db)
    post, elsewhere = await make_post(db, group_id=group.id), await make_post(db)
    admin = await make_admin(db)
    subject = await make_user(db, post_id=post.id)
    at_group = await make_user(db, user_type=UserType.ADMINISTRATOR)
    await give(db, at_group, [P.USER_VIEW, P.CERTIFICATE_VIEW], Node.group(group.id))
    at_other_post = await make_user(db, user_type=UserType.ADMINISTRATOR)
    await give(db, at_other_post, [P.USER_VIEW, P.CERTIFICATE_VIEW], Node.post(elsewhere.id))
    users_only = await make_user(db, user_type=UserType.ADMINISTRATOR)
    await give(db, users_only, [P.USER_VIEW], Node.post(post.id))
    nobody = await make_user(db, [P.POST_VIEW], post_id=post.id)
    hub = await _hub(engine)
    streams = {name: await _attach(hub, db, user) for name, user in (
        ("subject", subject), ("at_group", at_group), ("at_other_post", at_other_post), ("users_only", users_only),
        ("nobody", nobody))}

    renamed = await client.patch(V1.path(UserRoutes.USER_BY_ID, user_id=subject.id), headers=headers(admin),
                                 data={"username_alias": "Seen"})
    assert renamed.status_code == 200, renamed.text
    issued = await issue(client, headers, admin, subject)
    assert issued.status_code == 201, issued.text
    assert await hub.tick() == 2
    user_changed = ("user.changed", {"subject_user_id": subject.id})
    certificate = ("certificate.changed", {"subject_user_id": subject.id,
                                           "certificate_id": issued.json()["certificate"]["id"]})
    received = {name: [(event, {k: v for k, v in data.items() if k not in ("id", "refetch_within_ms")})
                       for event, data in _received(stream)] for name, stream in streams.items()}
    assert received == {"subject": [user_changed, certificate], "at_group": [user_changed, certificate],
                        "at_other_post": [], "users_only": [user_changed], "nobody": []}


async def test_role_and_dictionary_events_reach_holders_at_any_node_in_effect(client, db, engine, headers,
                                                                             events_on):
    post, archived = await make_post(db), await make_post(db)
    admin = await make_admin(db)
    at_post = await make_user(db, user_type=UserType.ADMINISTRATOR)
    await give(db, at_post, [P.ROLE_VIEW, P.TAG_VIEW], Node.post(post.id))
    at_archived = await make_user(db, user_type=UserType.ADMINISTRATOR)
    await give(db, at_archived, [P.ROLE_VIEW, P.TAG_VIEW], Node.post(archived.id))
    archived.is_deleted = True
    db.add(archived)
    await db.commit()
    plain = await make_user(db, [P.POST_VIEW], post_id=post.id)
    hub = await _hub(engine)
    streams = {name: await _attach(hub, db, user)
               for name, user in (("at_post", at_post), ("at_archived", at_archived), ("plain", plain))}
    h = headers(admin)
    role = await client.post(V1.path(RoleRoutes.ROOT), headers=h,
                             json={"name": "seen_role", "display_name": "Seen", "kind": "tree"})
    assert role.status_code == 201, role.text
    for path, body in ((V1.path(TagRoutes.CREATE), {"name": "seen"}),
                       (V1.path(CategoryRoutes.CREATE), {"name": "Seen", "alias": "seen"}),
                       (V1.path(GuideRoutes.ROOT), {"title": "Seen guide"})):
        assert (await client.post(path, headers=h, json=body)).status_code in (200, 201)
    assert await hub.tick() == 4
    events = {name: [(event, data.get("role_id", data.get("dictionary"))) for event, data in _received(stream)]
              for name, stream in streams.items()}
    assert events == {
        "at_post": [("role.changed", role.json()["id"]), ("dictionary.changed", "tags"),
                    ("dictionary.changed", "guides")],
        "at_archived": [("dictionary.changed", "guides")],      # a grant at an archived post counts nowhere
        "plain": [("dictionary.changed", "guides")],            # the guides: every stream
    }


# ---------------------------------------------------------------- the hub reloads the grants at once

async def test_an_assignment_moves_the_rights_of_a_stream_in_the_same_tick(client, db, engine, headers, events_on):
    """The new holder of role.view gets the role.changed that follows its assignment in the same tick, without the
    30 s rights refresh"""
    post = await make_post(db)
    admin = await make_admin(db)
    newcomer = await make_user(db, post_id=post.id, user_type=UserType.ADMINISTRATOR)
    await give(db, newcomer, [P.POST_VIEW], Node.post(post.id))
    viewer_role = await make_role(db, [P.ROLE_VIEW])
    hub = await _hub(engine)
    stream = await _attach(hub, db, newcomer)
    h = headers(admin)
    given = await client.post(V1.path(UserRoutes.USER_ASSIGNMENTS, user_id=newcomer.id), headers=h,
                              json={"role_id": viewer_role.id, "scope": {"kind": "post", "id": post.id}})
    assert given.status_code == 201, given.text
    created = await client.post(V1.path(RoleRoutes.ROOT), headers=h,
                                json={"name": "after_grant", "display_name": "After", "kind": "tree"})
    assert created.status_code == 201, created.text
    assert await hub.tick() == 2
    assert [event for event, _ in _received(stream)] == ["user.changed", "role.changed", "rights_changed"], \
        "its own user.changed and the rights_changed of the same tick both come (one refetch of GET /auth/me serves)"


async def test_a_role_change_moves_the_rights_of_its_holders(client, db, engine, headers, events_on):
    """A permission added to a role a connected user holds puts it in effect for that stream at once: the
    dictionary.changed of the same tick reaches them; removing it takes it away, and the role.changed of the removal
    still reaches them (they held role.view before it)"""
    admin = await make_admin(db)
    holder = await make_user(db, user_type=UserType.ADMINISTRATOR)
    (role,) = await give(db, holder, [P.ROLE_VIEW])
    hub = await _hub(engine)
    stream = await _attach(hub, db, holder)
    h = headers(admin)
    tag_view = (await db.exec(select(Permission).where(Permission.name == "tag.view"))).one()
    role_view = (await db.exec(select(Permission).where(Permission.name == "role.view"))).one()
    added = await client.put(V1.path(RoleRoutes.ROLE_PERMISSION_BY_ID, role_id=role.id, permission_id=tag_view.id),
                             headers=h)
    assert added.status_code == 200, added.text
    assert (await client.post(V1.path(TagRoutes.CREATE), headers=h, json={"name": "fresh"})).status_code in (200, 201)
    assert await hub.tick() == 2
    assert [event for event, _ in _received(stream)] == ["role.changed", "dictionary.changed", "rights_changed"]
    stream.queue.clear()                    # read by the client (a role.changed of the same role would conflate)

    removed = await client.delete(V1.path(RoleRoutes.ROLE_PERMISSION_BY_ID, role_id=role.id,
                                          permission_id=role_view.id), headers=h)
    assert removed.status_code == 200, removed.text
    assert await hub.tick() == 1
    assert [(event, data.get("role_id")) for _, _, event, data in queued(stream)] == [
        ("role.changed", role.id), ("rights_changed", None)], "the removal reaches those who held role.view before it"
    stream.queue.clear()
    other = await client.post(V1.path(RoleRoutes.ROOT), headers=h,
                              json={"name": "unseen", "display_name": "Unseen", "kind": "tree"})
    assert other.status_code == 201, other.text
    assert await hub.tick() == 1
    assert queued(stream) == [], "the next role.changed no longer does"


# ---------------------------------------------------------------- rights_changed: no event names the holder

def _events(connection) -> list:
    return [event for _, _, event, _ in queued(connection)[1:]]


async def test_a_role_permission_change_tells_its_holders_without_role_view(client, db, engine, headers, events_on):
    """Owner, 2026-11-22: the holders of a role whose permissions change refetch their rights though they cannot read
    roles; those who do not hold it, and those whose rights in effect stay the same (the permission comes through
    another role too), get nothing"""
    admin = await make_admin(db)
    role = await make_role(db, [P.POST_VIEW])
    holder = await make_user(db, user_type=UserType.VIEWER)
    await assign(db, holder, role, ROOT)
    twice = await make_user(db, user_type=UserType.VIEWER)
    await assign(db, twice, role, ROOT)
    await assign(db, twice, await make_role(db, [P.OBSERVATION_VIEW]), ROOT)
    stranger = await make_user(db, user_type=UserType.VIEWER)
    await give(db, stranger, [P.POST_VIEW])
    watcher = await make_user(db, user_type=UserType.ADMINISTRATOR)
    await give(db, watcher, [P.ROLE_VIEW])
    hub = await _hub(engine)
    streams = {name: await _attach(hub, db, user) for name, user in (
        ("holder", holder), ("twice", twice), ("stranger", stranger), ("watcher", watcher))}
    observation_view = (await db.exec(select(Permission).where(Permission.name == "observation.view"))).one()
    url = V1.path(RoleRoutes.ROLE_PERMISSION_BY_ID, role_id=role.id, permission_id=observation_view.id)

    added = await client.put(url, headers=headers(admin))
    assert added.status_code == 200, added.text
    assert await hub.tick() == 1
    assert {name: _events(stream) for name, stream in streams.items()} == {
        "holder": ["rights_changed"], "twice": [], "stranger": [], "watcher": ["role.changed"]}
    refetch = queued(streams["holder"])[-1][3]["refetch_within_ms"]
    assert refetch == hub._refetch_window(1), "spread over the streams told"

    for stream in streams.values():
        stream.queue.clear()
    removed = await client.delete(url, headers=headers(admin))
    assert removed.status_code == 200, removed.text
    assert await hub.tick() == 1
    assert {name: [event for _, _, event, _ in queued(stream)] for name, stream in streams.items()} == {
        "holder": ["rights_changed"], "twice": [], "stranger": [], "watcher": ["role.changed"]}


async def test_a_permission_outside_the_type_changes_nothing_in_effect(client, db, engine, headers, events_on):
    """A role holding a permission beyond a holder's type (written before the type allowed less): removing it changes
    nothing in effect for that holder, so no rights_changed"""
    from tests.helpers import _grant
    admin = await make_admin(db)
    role = await make_role(db, [P.POST_VIEW])
    viewer = await make_user(db, user_type=UserType.VIEWER)
    await assign(db, viewer, role, ROOT)
    await _grant(db, role, ["post.update"])         # outside the viewer boundary: never in effect for them
    await db.commit()
    hub = await _hub(engine)
    stream = await _attach(hub, db, viewer)
    post_update = (await db.exec(select(Permission).where(Permission.name == "post.update"))).one()
    removed = await client.delete(V1.path(RoleRoutes.ROLE_PERMISSION_BY_ID, role_id=role.id,
                                          permission_id=post_update.id), headers=headers(admin))
    assert removed.status_code == 200, removed.text
    assert await hub.tick() == 1
    assert _events(stream) == []


async def test_an_archive_tells_who_loses_rights_there_without_seeing_the_post(client, db, engine, headers,
                                                                              events_on):
    """A holder of observation.view at a post (no post.view: the scope.changed is not theirs) learns at once that its
    grant there stopped applying"""
    post = await make_post(db)
    admin = await make_admin(db)
    observer = await make_user(db, user_type=UserType.VIEWER)
    await give(db, observer, [P.OBSERVATION_VIEW], Node.post(post.id))
    hub = await _hub(engine)
    stream = await _attach(hub, db, observer)
    archived = await client.post(V1.path(PostRoutes.POST_ARCHIVE, post_id=post.id), headers=headers(admin))
    assert archived.status_code == 200, archived.text
    await hub.tick()
    assert _events(stream) == ["rights_changed"]


def _outbox_row(row_id: int, type_: str, payload: str):
    from datetime import datetime
    from app.backend.services.events.reader import OutboxRow
    return OutboxRow(id=row_id, type=type_, v=1, scope_key="*", permission=None, user_id=None, conflate_key=None,
                     payload=payload, occurred_at=datetime(2026, 11, 22), lag_ms=0)


def test_whose_grants_a_tick_reloads():
    from app.backend.services.events.hub import EVERY_USER, grants_to_reload
    assert grants_to_reload([_outbox_row(1, "scope.changed", "{}")]) == set()
    assert grants_to_reload([_outbox_row(1, "user.changed", '{"subject_user_id":4}'),
                             _outbox_row(2, "user.changed", '{"subject_user_id":6}')]) == {4, 6}
    assert grants_to_reload([_outbox_row(1, "user.changed", '{"subject_user_id":4}'),
                             _outbox_row(2, "role.changed", '{"role_id":1}')]) is EVERY_USER
    assert grants_to_reload([_outbox_row(1, "user.changed", "not json"),
                             _outbox_row(2, "user.changed", "{}")]) == set(), "a bad row never stops the pump"


async def test_an_older_load_never_replaces_a_newer_one(engine, events_on):
    """The rights refresh and the pump load in two tasks: a refresh that began before a reload of the pump and ends
    after it leaves the newer grants and tree in place"""
    from app.backend.auth.scope import ScopeTree
    from tests.events.test_event_hub import auth_of, grant
    hub = await _hub(engine)
    hub.attach(auth_of(1), later(), None)
    older, newer = hub._next_load(), hub._next_load()
    hub._set_grants([1], {1: (grant(ROOT, P.ROLE_VIEW),)}, newer)
    hub._set_grants([1], {1: ()}, older)
    assert list(hub.rights.holders("*", P.ROLE_VIEW.value)) == [1]
    fresh = ScopeTree(group_parent={}, archived_groups=frozenset(), post_group={7: None}, archived_posts=frozenset())
    hub._set_tree(fresh, hub._next_load())
    hub._set_tree(ScopeTree.empty(), older)
    assert hub.rights.tree is fresh
    hub._set_grants([1], {1: ()}, hub._next_load())
    assert list(hub.rights.holders("*", P.ROLE_VIEW.value)) == []


async def test_a_reload_for_users_not_connected_reads_nothing(engine, events_on):
    hub = await _hub(engine)

    async def never(*args, **kwargs):
        raise AssertionError("no query for users not connected")

    hub._load_grants = never
    await hub.reload_grants([5, 6])
    await hub.reload_grants(None)
