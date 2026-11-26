"""publish(): a row in the transaction of the change, only when it commits, params from the catalog only; and the
places that publish: moves and the scope tree, one event per operation."""
import json

import pytest
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.auth.scope import Node, ROOT
from app.backend.config import EventsConfig, settings
from app.backend.enums.events import EventType
from app.backend.enums.general import Permissions
from app.backend.enums.routes import (BrigadeRoutes, CorpsRoutes, MediaRoutes, MoveRoutes, PostRoutes,
                                      ScopeGroupRoutes)
from app.backend.models.outbox_event import OutboxEvent
from app.backend.services.events import publish
from app.backend.services.events.hub import EventHub
from tests.helpers import make_admin, make_group, make_post, make_user
from tests.events.test_event_hub import TextRenderer, auth_of, grant, later, queued
from tests.media.test_media_uploads import storage  # noqa: F401
from tests.units.test_queued_moves import DATA, _move_body, _scene, _waiting_move
from tests.media.test_upload_archive_cancel import _observation, _open_session

pytestmark = pytest.mark.raw_moves      # requests as written (tests/conftest.py _FormClient)


@pytest.fixture
def events_on(monkeypatch):
    monkeypatch.setattr(settings.events, "enabled", True)


async def _rows(db) -> list:
    return list((await db.exec(select(OutboxEvent).order_by(OutboxEvent.id)
                               .execution_options(populate_existing=True))).all())


def _summary(rows) -> list:
    return [(row.type, row.scope_key, row.permission, row.user_id, json.loads(row.payload)) for row in rows]


# ---------------------------------------------------------------- publish itself

async def test_a_row_commits_with_the_change(db, events_on):
    publish(db, EventType.MOVE_CHANGED, scope=Node.post(7), permission=Permissions.POST_UPDATE, user_id=3,
            move_id=9, move_status="done")
    await db.commit()
    (row,) = await _rows(db)
    assert (row.type, row.v, row.scope_key, row.permission, row.user_id) == ("move.changed", 1, "p:7", "post.update", 3)
    assert json.loads(row.payload) == {"move_id": 9, "move_status": "done"}
    assert row.id is not None and row.occurred_at is not None


async def test_a_rollback_leaves_no_event(db, events_on):
    publish(db, EventType.SCOPE_CHANGED, scope=Node.group(2), permission=Permissions.POST_VIEW)
    await db.rollback()
    assert await _rows(db) == []


async def test_off_writes_nothing(db):
    assert settings.events.enabled is False
    publish(db, EventType.SCOPE_CHANGED, scope=Node.group(2), permission=Permissions.POST_VIEW)
    await db.commit()
    assert await _rows(db) == []


async def test_params_are_checked_whether_on_or_off(db):
    with pytest.raises(TypeError):
        publish(db, EventType.MOVE_CHANGED, scope=Node.post(1), permission=Permissions.POST_UPDATE, move_id=1)
    with pytest.raises(TypeError):
        publish(db, EventType.MOVE_CHANGED, scope=Node.post(1), permission=Permissions.POST_UPDATE, move_id="x",
                move_status="done")
    with pytest.raises(TypeError):
        publish(db, EventType.MOVE_CHANGED, scope=Node.post(1), permission=Permissions.POST_UPDATE, move_id=1,
                move_status="somewhere")
    with pytest.raises(TypeError):
        publish(db, EventType.SCOPE_CHANGED, scope=Node.post(1), permission=Permissions.POST_VIEW, note="free text")


async def test_a_target_is_required(db, events_on):
    with pytest.raises(TypeError):
        publish(db, EventType.SCOPE_CHANGED)
    with pytest.raises(TypeError):
        publish(db, EventType.SCOPE_CHANGED, scope=ROOT)
    publish(db, EventType.MOVE_CHANGED, user_id=4, move_id=1, move_status="failed")     # personal only
    await db.commit()
    (row,) = await _rows(db)
    assert row.scope_key is None and row.permission is None and row.user_id == 4


# ---------------------------------------------------------------- moves

async def test_an_immediate_post_move_publishes_the_new_and_the_old_place(client, db, headers, events_on):
    admin = await make_admin(db)
    home, there = await make_group(db), await make_group(db)
    post = await make_post(db, group_id=home.id)
    moved = await client.put(V1.path(PostRoutes.POST_BY_ID, post_id=post.id),
                             json={"group_id": there.id, "expected_group_id": home.id}, headers=headers(admin))
    assert moved.status_code == 200, moved.text
    assert _summary(await _rows(db)) == [
        ("scope.changed", f"p:{post.id}", "post.view", None, {}),
        ("scope.changed", f"g:{home.id}", "post.view", None, {}),
    ]


async def test_a_post_moved_from_the_root_publishes_the_root(client, db, headers, events_on):
    admin = await make_admin(db)
    there = await make_group(db)
    post = await make_post(db)
    moved = await client.put(V1.path(PostRoutes.POST_BY_ID, post_id=post.id),
                             json={"group_id": there.id, "expected_group_id": None}, headers=headers(admin))
    assert moved.status_code == 200, moved.text
    assert [row.scope_key for row in await _rows(db)] == [f"p:{post.id}", "root"]


async def test_a_refused_move_publishes_nothing(client, db, headers, events_on):
    home, there = await make_group(db), await make_group(db)
    post = await make_post(db, group_id=home.id)
    clerk = await make_user(db, [Permissions.POST_VIEW, Permissions.POST_UPDATE], post_id=post.id)
    refused = await client.put(V1.path(PostRoutes.POST_BY_ID, post_id=post.id),
                               json={"group_id": there.id, "expected_group_id": home.id}, headers=headers(clerk))
    assert refused.status_code == 403, refused.text
    stale = await client.put(V1.path(PostRoutes.POST_BY_ID, post_id=post.id),
                             json={"group_id": there.id, "expected_group_id": there.id},
                             headers=headers(await make_admin(db)))
    assert stale.status_code == 409, stale.text
    assert await _rows(db) == []


async def test_a_plain_edit_publishes_nothing(client, db, headers, events_on):
    admin = await make_admin(db)
    post = await make_post(db)
    renamed = await client.put(V1.path(PostRoutes.POST_BY_ID, post_id=post.id), json={"name": "Renamed"},
                               headers=headers(admin))
    assert renamed.status_code == 200, renamed.text
    assert await _rows(db) == []


async def test_a_waiting_move_its_run_and_its_requester(client, db, headers, storage, events_on):
    scene = await _scene(client, db, headers)
    move_id = await _waiting_move(client, headers, scene)
    assert _summary(await _rows(db)) == [
        ("move.changed", f"p:{scene.post.id}", "post.update", scene.admin.id, {"move_id": move_id, "move_status": "waiting"}),
    ]
    aborted = await client.post(V1.path(MediaRoutes.UPLOAD_SESSION_ABORT, upload_id=scene.upload_id),
                                headers=headers(scene.admin))
    assert aborted.status_code == 200, aborted.text
    assert _summary((await _rows(db))[1:]) == [
        ("move.changed", f"p:{scene.post.id}", "post.update", scene.admin.id, {"move_id": move_id, "move_status": "done"}),
        ("scope.changed", f"p:{scene.post.id}", "post.view", None, {}),
        ("scope.changed", f"g:{scene.home.id}", "post.view", None, {}),
    ]


async def test_a_cancel_publishes_cancelled(client, db, headers, storage, events_on):
    scene = await _scene(client, db, headers)
    move_id = await _waiting_move(client, headers, scene)
    cancelled = await client.post(V1.path(MoveRoutes.MOVE_CANCEL, move_id=move_id), headers=headers(scene.admin))
    assert cancelled.status_code == 200, cancelled.text
    last = (await _rows(db))[-1]
    assert (last.type, json.loads(last.payload)) == ("move.changed", {"move_id": move_id, "move_status": "cancelled"})
    assert len(await _rows(db)) == 2


async def test_a_forced_move_publishes_done_and_the_places(client, db, headers, storage, events_on):
    scene = await _scene(client, db, headers)
    forced = await client.put(V1.path(PostRoutes.POST_BY_ID, post_id=scene.post.id),
                              json=_move_body(scene, force=True), headers=headers(scene.admin))
    assert forced.status_code == 200, forced.text
    summary = _summary(await _rows(db))
    assert [(kind, key) for kind, key, *_ in summary] == [
        ("move.changed", f"p:{scene.post.id}"), ("scope.changed", f"p:{scene.post.id}"),
        ("scope.changed", f"g:{scene.home.id}")]
    assert summary[0][4]["move_status"] == "done"


async def test_a_brigade_move_publishes_the_brigade_and_its_old_corps(client, db, headers, events_on):
    admin = await make_admin(db)
    corps, other = await make_group(db, kind="corps"), await make_group(db, kind="corps")
    brigade = await make_group(db, parent_id=corps.id, kind="brigade")
    moved = await client.patch(V1.path(BrigadeRoutes.BRIGADE_BY_ID, brigade_id=brigade.id),
                               json={"corps_id": other.id, "expected_corps_id": corps.id}, headers=headers(admin))
    assert moved.status_code == 200, moved.text
    assert [row.scope_key for row in await _rows(db)] == [f"g:{brigade.id}", f"g:{corps.id}"]


# ---------------------------------------------------------------- creates: one event, at the new node's parent

@pytest.mark.parametrize("in_group", [True, False])
async def test_a_post_create_publishes_its_group(client, db, headers, events_on, in_group):
    admin = await make_admin(db)
    group = await make_group(db) if in_group else None
    created = await client.post(V1.path(PostRoutes.ROOT), headers=headers(admin), json={
        "name": "New", "code": f"NEW-{in_group}", "group_id": group.id if group else None})
    assert created.status_code == 200, created.text
    assert _summary(await _rows(db)) == [("scope.changed", f"g:{group.id}" if group else "root", "post.view", None, {})]


@pytest.mark.parametrize("in_group", [True, False])
async def test_a_group_create_publishes_its_parent(client, db, headers, events_on, in_group):
    admin = await make_admin(db)
    parent = await make_group(db) if in_group else None
    created = await client.post(V1.path(ScopeGroupRoutes.ROOT), headers=headers(admin),
                                json={"name": "Inner", "parent_id": parent.id if parent else None})
    assert created.status_code == 200, created.text
    assert _summary(await _rows(db)) == [("scope.changed", f"g:{parent.id}" if parent else "root", "post.view", None,
                                          {})]


async def test_a_brigade_create_publishes_its_corps_and_a_corps_create_the_root(client, db, headers, events_on):
    admin = await make_admin(db)
    corps = await client.post(V1.path(CorpsRoutes.ROOT), headers=headers(admin), json={"name": "Corps A"})
    assert corps.status_code == 201, corps.text
    brigade = await client.post(V1.path(BrigadeRoutes.ROOT), headers=headers(admin),
                                json={"name": "Brigade A", "corps_id": corps.json()["id"]})
    assert brigade.status_code == 201, brigade.text
    assert [(row.type, row.scope_key) for row in await _rows(db)] == [
        ("scope.changed", "root"), ("scope.changed", f"g:{corps.json()['id']}")]


async def test_a_refused_create_publishes_nothing(client, db, headers, events_on):
    admin = await make_admin(db)
    taken = await make_post(db)
    group = await make_group(db)
    for path, body in ((V1.path(PostRoutes.ROOT), {"name": "Again", "code": taken.code}),
                       (V1.path(ScopeGroupRoutes.ROOT), {"name": group.name}),
                       (V1.path(CorpsRoutes.ROOT), {"name": group.name})):
        refused = await client.post(path, headers=headers(admin), json=body)
        assert refused.status_code in (400, 409), refused.text      # post_code_taken is 400, a name taken 409
    clerk = await make_user(db, [Permissions.POST_VIEW, Permissions.POST_CREATE], post_id=taken.id)
    forbidden = await client.post(V1.path(PostRoutes.ROOT), headers=headers(clerk),
                                  json={"name": "Mine", "code": "MINE", "group_id": group.id})
    assert forbidden.status_code == 403, forbidden.text
    assert await _rows(db) == []


async def test_the_create_event_lets_the_hub_deliver_events_of_the_new_post_at_once(client, db, engine, headers,
                                                                                     events_on):
    """The hub reloads its tree on the scope.changed of the create: the next event of the new post reaches a holder
    at its group without waiting for the rights refresh (an unknown post sits under the root only)"""
    admin = await make_admin(db)
    group, other = await make_group(db), await make_group(db)
    hub = EventHub(engine, TextRenderer(), EventsConfig(enabled=True), workers=1)
    await hub.start(run_tasks=False)
    at_group = hub.attach(auth_of(1, grant(Node.group(group.id), Permissions.POST_VIEW, Permissions.POST_UPDATE)),
                          later(), None)
    at_other = hub.attach(auth_of(2, grant(Node.group(other.id), Permissions.POST_VIEW, Permissions.POST_UPDATE)),
                          later(), None)
    created = await client.post(V1.path(PostRoutes.ROOT), headers=headers(admin),
                                json={"name": "Fresh", "code": "FRESH", "group_id": group.id})
    assert created.status_code == 200, created.text
    assert await hub.tick() == 1
    assert [event for _, _, event, _ in queued(at_group)] == ["ready", "scope.changed", "rights_changed"], \
        "the new post is in the post_permissions of the holder at its group (GET /auth/me)"
    assert [event for _, _, event, _ in queued(at_other)] == ["ready"]

    publish(db, EventType.MOVE_CHANGED, scope=Node.post(created.json()["id"]), permission=Permissions.POST_UPDATE,
            move_id=1, move_status="waiting")
    await db.commit()
    assert await hub.tick() == 1
    assert [event for _, _, event, _ in queued(at_group)][-1] == "move.changed"
    assert [event for _, _, event, _ in queued(at_other)] == ["ready"]


# ---------------------------------------------------------------- archive and restore: one event, the top node

async def test_a_post_archive_and_restore(client, db, headers, events_on):
    admin = await make_admin(db)
    post = await make_post(db)
    for route in (PostRoutes.POST_ARCHIVE, PostRoutes.POST_RESTORE):
        answer = await client.post(V1.path(route, post_id=post.id), headers=headers(admin))
        assert answer.status_code == 200, answer.text
    assert _summary(await _rows(db)) == [("scope.changed", f"p:{post.id}", "post.view", None, {})] * 2


async def test_a_post_archive_cancels_its_waiting_move_with_an_event(client, db, headers, storage, events_on):
    scene = await _scene(client, db, headers)
    move_id = await _waiting_move(client, headers, scene)
    archived = await client.post(V1.path(PostRoutes.POST_ARCHIVE, post_id=scene.post.id), headers=headers(scene.admin))
    assert archived.status_code == 200, archived.text
    rows = (await _rows(db))[1:]
    assert [(row.type, row.scope_key) for row in rows] == [("move.changed", f"p:{scene.post.id}"),
                                                           ("scope.changed", f"p:{scene.post.id}")]
    assert json.loads(rows[0].payload) == {"move_id": move_id, "move_status": "cancelled"}


async def test_a_cascade_archive_publishes_once(client, db, headers, events_on):
    admin = await make_admin(db)
    corps = await make_group(db, kind="corps")
    brigade = await make_group(db, parent_id=corps.id, kind="brigade")
    await make_post(db, group_id=brigade.id)
    await make_post(db, group_id=brigade.id)
    archived = await client.post(V1.path(BrigadeRoutes.BRIGADE_ARCHIVE, brigade_id=brigade.id),
                                 params={"cascade": "true"}, headers=headers(admin))
    assert archived.status_code == 200, archived.text
    restored = await client.post(V1.path(BrigadeRoutes.BRIGADE_RESTORE, brigade_id=brigade.id), headers=headers(admin))
    assert restored.status_code == 200, restored.text
    assert [row.scope_key for row in await _rows(db)] == [f"g:{brigade.id}"] * 2


async def test_a_group_archive_and_restore(client, db, headers, events_on):
    admin = await make_admin(db)
    group = await make_group(db)
    for route in (ScopeGroupRoutes.GROUP_ARCHIVE, ScopeGroupRoutes.GROUP_RESTORE):
        answer = await client.post(V1.path(route, group_id=group.id), headers=headers(admin))
        assert answer.status_code == 200, answer.text
    assert [row.scope_key for row in await _rows(db)] == [f"g:{group.id}"] * 2


async def test_an_immediate_group_move_publishes_the_new_and_the_old_place(client, db, headers, events_on):
    admin = await make_admin(db)
    top, there = await make_group(db), await make_group(db)
    middle = await make_group(db, parent_id=top.id)
    moved = await client.patch(V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=middle.id),
                               json={"parent_id": there.id, "expected_parent_id": top.id}, headers=headers(admin))
    assert moved.status_code == 200, moved.text
    assert [row.scope_key for row in await _rows(db)] == [f"g:{middle.id}", f"g:{top.id}"]


async def test_a_refused_archive_publishes_nothing(client, db, headers, events_on):
    admin = await make_admin(db)
    group = await make_group(db)
    await make_post(db, group_id=group.id)
    refused = await client.post(V1.path(ScopeGroupRoutes.GROUP_ARCHIVE, group_id=group.id), headers=headers(admin))
    assert refused.status_code == 409, refused.text
    assert await _rows(db) == []


async def test_upload_progress_is_no_event(client, db, headers, storage, events_on):
    admin = await make_admin(db)
    post = await make_post(db)
    await _open_session(client, headers, admin, await _observation(db, admin, post), DATA, 10)
    assert await _rows(db) == []
