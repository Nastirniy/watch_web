"""Reading the audit log (part 6, task 5: the system view): GET /audit-logs/ (filters of decision 10, newest first,
before_id keyset), GET /audit-logs/{audit_id}, GET /audit-logs/events (the catalog). Every entry is one model per
event (a union discriminated by event) with typed params; rows the catalog cannot describe are legacy.unknown."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event as sa_event

from app.backend.api.v1 import V1
from app.backend.auth.scope import Node, ROOT
from app.backend.config import settings
from app.backend.enums.audit import SWITCHABLE, AuditCategory, AuditEvent
from app.backend.enums.general import Permissions
from app.backend.enums.routes import AuditRoutes, TagRoutes
from app.backend.models.audit import AuditLog
from app.backend.models.role_assignment import scope_columns
from tests.helpers import make_admin, make_group, make_post, make_user

ENTRIES = V1.path(AuditRoutes.ROOT)
EVENTS = V1.path(AuditRoutes.EVENTS)
NOW = datetime.now(timezone.utc).replace(microsecond=0)


def _entry(audit_id: int) -> str:
    return V1.path(AuditRoutes.ENTRY_BY_ID, audit_id=audit_id)


def _camel(code: str) -> str:
    return "".join(part.capitalize() for part in code.replace(".", "_").split("_"))


async def add_row(db, event, params=None, scope=None, code=None, **columns) -> AuditLog:
    """An audit row as create_audit_log writes it (event None: an old row; code: a code the catalog does not know)"""
    if event is not None:
        resource_type, action = event.code.split(".", 1)
        columns.setdefault("resource_type", resource_type)
        columns.setdefault("action", action)
        columns.setdefault("category", event.category.value)
    row = AuditLog(event=event.code if event is not None else code, params=params,
                   **(scope_columns(scope) if scope is not None else {}), **columns)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def _ids(client, h, **params) -> list:
    response = await client.get(ENTRIES, params={"page_size": 200, **params}, headers=h)
    assert response.status_code == 200, response.text
    return [entry["id"] for entry in response.json()]


# ---------------------------------------------------------------- list

async def test_list_returns_newest_first_with_actor_and_scope(client, db, headers):
    admin = await make_admin(db)
    h = headers(admin)
    post = await make_post(db, name="Post North")
    group = await make_group(db, name="Sector East")
    first = await add_row(db, AuditEvent.TAG_CREATE, {"name": "Red"}, user_id=admin.id)
    second = await add_row(db, AuditEvent.POST_UPDATE, {"post_name": "Post North", "post_code": "PN"},
                           scope=Node.post(post.id), user_id=admin.id, resource_id=post.id,
                           old_values={"name": "Post N"}, new_values={"name": "Post North"})
    third = await add_row(db, AuditEvent.SCOPE_GROUP_UPDATE, {"group_name": "Sector East", "group_kind": "group"},
                          scope=Node.group(group.id), user_id=987654)
    fourth = await add_row(db, AuditEvent.USER_UPDATE, {"username": "someone"}, scope=ROOT,
                           request_id="ab" * 16, session_id="sess-1", ip_address="10.0.0.1",
                           user_agent="agent", request_url="http://test/x")

    response = await client.get(ENTRIES, headers=h)
    assert response.status_code == 200, response.text
    entries = response.json()
    assert [e["id"] for e in entries] == [fourth.id, third.id, second.id, first.id]

    by_id = {e["id"]: e for e in entries}
    assert by_id[first.id]["event"] == "tag.create" and by_id[first.id]["params"] == {"name": "Red"}
    assert by_id[first.id]["category"] == AuditCategory.DICTIONARIES.value
    assert by_id[first.id]["actor"] == {"id": admin.id, "username": admin.username}
    assert by_id[first.id]["scope"] is None
    assert by_id[second.id]["scope"] == {"kind": "post", "id": post.id, "name": "Post North"}
    assert by_id[second.id]["resource_type"] == "post" and by_id[second.id]["resource_id"] == post.id
    assert by_id[second.id]["old_values"] == {"name": "Post N"}
    assert by_id[second.id]["new_values"] == {"name": "Post North"}
    assert by_id[third.id]["scope"] == {"kind": "group", "id": group.id, "name": "Sector East"}
    assert by_id[third.id]["actor"] == {"id": 987654, "username": None}   # an id no user has
    assert by_id[fourth.id]["actor"] is None
    assert by_id[fourth.id]["scope"] == {"kind": "root", "id": None, "name": settings.app.name}
    # the system view shows the device fields
    assert {k: by_id[fourth.id][k] for k in ("request_id", "session_id", "ip_address", "user_agent", "request_url")} \
        == {"request_id": "ab" * 16, "session_id": "sess-1", "ip_address": "10.0.0.1", "user_agent": "agent",
            "request_url": "http://test/x"}
    assert by_id[fourth.id]["success"] is True and by_id[fourth.id]["created_at"]


async def test_a_written_row_reads_back(client, db, headers):
    admin = await make_admin(db)
    h = headers(admin)
    created = await client.post(V1.path(TagRoutes.CREATE), json={"name": "Blue"}, headers=h)
    assert created.status_code == 200, created.text
    (entry,) = (await client.get(ENTRIES, params={"event": "tag.create"}, headers=h)).json()
    assert entry["params"] == {"name": created.json()["name"]}
    assert entry["actor"] == {"id": admin.id, "username": admin.username}
    assert entry["resource_id"] == created.json()["id"]
    assert entry["request_id"] and len(entry["request_id"]) == 32


async def test_optional_params_are_absent_never_null(client, db, headers):
    h = headers(await make_admin(db))
    row = await add_row(db, AuditEvent.SESSION_END, {"username": "u1", "cause": "admin"}, scope=ROOT)
    entry = (await client.get(_entry(row.id), headers=h)).json()
    assert entry["params"] == {"username": "u1", "cause": "admin"}


async def test_rows_the_catalog_cannot_describe_are_legacy_unknown(client, db, headers):
    h = headers(await make_admin(db))
    old = await add_row(db, None, action="frobnicate", resource_type="widget", details={"message": "Old text"})
    bare = await add_row(db, None, action="poke", resource_type="widget")
    # a row migration 031 backfilled: its event is known, its params were never written
    backfilled = await add_row(db, AuditEvent.USER_LOGIN, None, details={"message": "User logged in"})
    gone = await add_row(db, None, code="thing.removed_later", action="removed_later", resource_type="thing",
                         params={"what": 1})

    entries = {e["id"]: e for e in (await client.get(ENTRIES, headers=h)).json()}
    assert entries[old.id]["event"] == "legacy.unknown"
    assert entries[old.id]["params"] == {"action": "frobnicate", "resource_type": "widget", "message": "Old text"}
    assert entries[bare.id]["params"] == {"action": "poke", "resource_type": "widget"}
    assert entries[backfilled.id]["event"] == "legacy.unknown"
    assert entries[backfilled.id]["params"] == {"action": "login", "resource_type": "user",
                                                "message": "User logged in"}
    assert entries[backfilled.id]["category"] == AuditCategory.AUTHENTICATION.value
    assert entries[gone.id]["event"] == "legacy.unknown"


# ---------------------------------------------------------------- filters

async def _dataset(db):
    """corps C > brigade B > post P1; corps C > post P0; plain group S > post P3; rows at each, at the root, without a
    scope and an old row"""
    corps = await make_group(db, name="Corps C", kind="corps")
    brigade = await make_group(db, name="Brigade B", parent_id=corps.id, kind="brigade")
    plain = await make_group(db, name="Sector S")
    p1 = await make_post(db, group_id=brigade.id)
    p0 = await make_post(db, group_id=corps.id)
    p3 = await make_post(db, group_id=plain.id)
    actor = await make_user(db, post_id=p1.id)
    rows = {
        "login": await add_row(db, AuditEvent.USER_LOGIN, {"username": actor.username}, scope=Node.post(p1.id),
                               user_id=actor.id, resource_id=actor.id, request_id="1f" * 16, session_id="sess-a",
                               created_at=NOW - timedelta(days=5)),
        "post": await add_row(db, AuditEvent.POST_UPDATE, {"post_name": "P0", "post_code": "P0"},
                              scope=Node.post(p0.id), resource_id=424242, created_at=NOW - timedelta(days=4)),
        "brigade": await add_row(db, AuditEvent.SCOPE_GROUP_UPDATE, {"group_name": "B", "group_kind": "brigade"},
                                 scope=Node.group(brigade.id), resource_id=brigade.id,
                                 created_at=NOW - timedelta(days=3)),
        "plain": await add_row(db, AuditEvent.SCOPE_GROUP_ARCHIVE, {"group_name": "S", "group_kind": "group"},
                               scope=Node.group(plain.id), resource_id=plain.id, created_at=NOW - timedelta(days=3)),
        "p3": await add_row(db, AuditEvent.OBSERVATION_CREATE,
                            {"post_name": "P3", "observation_id": 5, "signal_type_code": "X"},
                            scope=Node.post(p3.id), resource_id=5, created_at=NOW - timedelta(days=2)),
        "refused": await add_row(db, AuditEvent.AUTH_CERTIFICATE_LOGIN,
                                 {"certificate_cn": "cn", "certificate_serial": "0a", "matched": False},
                                 scope=ROOT, success=False, created_at=NOW - timedelta(days=1)),
        "tag": await add_row(db, AuditEvent.TAG_CREATE, {"name": "T"}, resource_id=77, created_at=NOW),
        "old": await add_row(db, None, action="frobnicate", resource_type="widget", created_at=NOW),
    }
    return {"corps": corps, "brigade": brigade, "plain": plain, "p1": p1, "p0": p0, "p3": p3, "actor": actor,
            "rows": rows}


FILTERS = [
    ("event", lambda d: {"event": ["user.login"]}, {"login"}),
    ("event_several", lambda d: {"event": ["post.update", "tag.create"]}, {"post", "tag"}),
    ("event_legacy", lambda d: {"event": ["legacy.unknown"]}, {"old"}),
    ("category", lambda d: {"category": ["structure"]}, {"post", "brigade", "plain"}),
    ("category_several", lambda d: {"category": ["authentication", "dictionaries"]}, {"login", "refused", "tag"}),
    ("user_id", lambda d: {"user_id": d["actor"].id}, {"login"}),
    ("resource_type", lambda d: {"resource_type": "scope_group"}, {"brigade", "plain"}),
    ("resource_id", lambda d: {"resource_id": 424242}, {"post"}),
    ("success", lambda d: {"success": "false"}, {"refused"}),
    ("created_from", lambda d: {"created_from": (NOW - timedelta(days=1, hours=12)).isoformat()},
     {"refused", "tag", "old"}),
    ("created_to", lambda d: {"created_to": (NOW - timedelta(days=3, hours=12)).isoformat()}, {"login", "post"}),
    ("request_id", lambda d: {"request_id": "1f" * 16}, {"login"}),
    ("session_id", lambda d: {"session_id": "sess-a"}, {"login"}),
    ("post_id", lambda d: {"post_id": d["p0"].id}, {"post"}),
    ("group_id", lambda d: {"group_id": d["brigade"].id}, {"login", "brigade"}),
    ("group_id_plain", lambda d: {"group_id": d["plain"].id}, {"plain", "p3"}),
    ("corps_id", lambda d: {"corps_id": d["corps"].id}, {"login", "post", "brigade"}),
    ("brigade_id", lambda d: {"brigade_id": d["brigade"].id}, {"login", "brigade"}),
    ("corps_id_of_a_brigade", lambda d: {"corps_id": d["brigade"].id}, set()),
    ("brigade_id_of_a_plain_group", lambda d: {"brigade_id": d["plain"].id}, set()),
    ("corps_and_post", lambda d: {"corps_id": d["corps"].id, "post_id": d["p0"].id}, {"post"}),
]


@pytest.mark.parametrize("params, expected", [(p, e) for _, p, e in FILTERS], ids=[n for n, _, _ in FILTERS])
async def test_each_filter_narrows_the_list(client, db, headers, params, expected):
    h = headers(await make_admin(db))
    data = await _dataset(db)
    names = {row.id: name for name, row in data["rows"].items()}
    found = await _ids(client, h, **params(data))
    assert {names[i] for i in found} == expected
    assert found == sorted(found, reverse=True)


async def test_before_id_pages_through_newest_first(client, db, headers):
    h = headers(await make_admin(db))
    rows = [await add_row(db, AuditEvent.TAG_CREATE, {"name": f"t{n}"}) for n in range(5)]
    ids = sorted((row.id for row in rows), reverse=True)
    first = await _ids(client, h, page_size=2)
    assert first == ids[:2]
    second = await _ids(client, h, page_size=2, before_id=first[-1])
    assert second == ids[2:4]
    third = await _ids(client, h, page_size=2, before_id=second[-1])
    assert third == ids[4:]
    assert await _ids(client, h, page_size=2, before_id=third[-1]) == []


async def test_page_size_is_at_most_200(client, db, headers):
    h = headers(await make_admin(db))
    response = await client.get(ENTRIES, params={"page_size": 201}, headers=h)
    assert response.status_code == 422


async def test_an_unknown_event_code_is_refused(client, db, headers):
    h = headers(await make_admin(db))
    response = await client.get(ENTRIES, params={"event": "no.such_event"}, headers=h)
    assert response.status_code == 422


async def test_a_page_costs_a_constant_number_of_queries(client, db, headers, engine):
    admin = await make_admin(db)
    h = headers(admin)

    async def add_rows(n: int, start: int) -> None:
        for k in range(start, start + n):
            post = await make_post(db)
            group = await make_group(db)
            actor = await make_user(db)
            await add_row(db, AuditEvent.TAG_CREATE, {"name": f"t{k}"}, user_id=actor.id, scope=Node.post(post.id))
            await add_row(db, AuditEvent.SCOPE_GROUP_UPDATE, {"group_name": "g", "group_kind": "group"},
                          user_id=actor.id, scope=Node.group(group.id))
            await add_row(db, AuditEvent.USER_UPDATE, {"username": "u"}, scope=ROOT, user_id=admin.id)

    statements = []

    def count(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    sa_event.listen(engine.sync_engine, "before_cursor_execute", count)
    try:
        await add_rows(3, 0)
        await client.get(ENTRIES, params={"page_size": 200}, headers=h)   # the first request touches the session
        statements.clear()
        first = await client.get(ENTRIES, params={"page_size": 200}, headers=h)
        assert first.status_code == 200, first.text
        small = len(statements)
        await add_rows(10, 3)
        statements.clear()
        second = await client.get(ENTRIES, params={"page_size": 200}, headers=h)
        assert second.status_code == 200, second.text
    finally:
        sa_event.remove(engine.sync_engine, "before_cursor_execute", count)
    assert len(second.json()) > len(first.json())
    assert len(statements) == small


# ---------------------------------------------------------------- one entry

async def test_one_entry_and_an_unknown_id(client, db, headers):
    h = headers(await make_admin(db))
    row = await add_row(db, AuditEvent.TAG_CREATE, {"name": "Red"})
    found = await client.get(_entry(row.id), headers=h)
    assert found.status_code == 200, found.text
    assert found.json()["id"] == row.id and found.json()["event"] == "tag.create"

    missing = await client.get(_entry(row.id + 1000), headers=h)
    assert missing.status_code == 404
    assert missing.json()["code"] == "audit_entry_not_found"
    assert missing.json()["audit_id"] == row.id + 1000


# ---------------------------------------------------------------- the catalog

async def test_events_lists_every_code_with_its_category_and_switchable(client, db, headers):
    h = headers(await make_admin(db))
    response = await client.get(EVENTS, headers=h)
    assert response.status_code == 200, response.text
    listed = {item["code"]: item for item in response.json()}
    assert set(listed) == {event.code for event in AuditEvent}
    for event in AuditEvent:
        assert listed[event.code]["category"] == event.category.value
        assert listed[event.code]["switchable"] is (event.category in SWITCHABLE)
        assert listed[event.code]["title"] == event.title
    assert listed["database.restore"]["switchable"] is False


# ---------------------------------------------------------------- access

async def test_without_audit_view_every_route_is_403(client, db, headers):
    h = headers(await make_user(db, [Permissions.POST_VIEW]))
    row = await add_row(db, AuditEvent.TAG_CREATE, {"name": "Red"})
    for path in (ENTRIES, EVENTS, _entry(row.id)):
        response = await client.get(path, headers=h)
        assert response.status_code == 403, path
        assert response.json()["code"] == "permission_denied"


# ---------------------------------------------------------------- OpenAPI

def test_openapi_has_one_entry_model_per_event():
    from app.main import fastapi_app
    document = V1.openapi(fastapi_app)
    components = document["components"]["schemas"]
    for event in AuditEvent:
        entry = components[f"{_camel(event.code)}AuditEntry"]
        assert entry["properties"]["event"].get("const") == event.code or \
            entry["properties"]["event"].get("enum") == [event.code]
        assert entry["properties"]["params"]["$ref"].endswith(f"/{_camel(event.code)}AuditParams")
        params = components[f"{_camel(event.code)}AuditParams"]
        assert set(params.get("properties", {})) == set(event.params)
    assert not [name for name in components if "__" in name]

    # openapi-typescript reads the union from oneOf + discriminator on event
    listed = document["paths"][ENTRIES]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    items = listed["items"]
    assert items["discriminator"]["propertyName"] == "event"
    assert set(items["discriminator"]["mapping"]) == {event.code for event in AuditEvent}
    assert len(items["oneOf"]) == len(AuditEvent)
    one = document["paths"][V1.path(AuditRoutes.ENTRY_BY_ID)]["get"]["responses"]["200"]["content"]
    assert one["application/json"]["schema"]["discriminator"]["propertyName"] == "event"


def test_optional_params_are_optional_in_the_schema():
    from app.main import fastapi_app
    params = V1.openapi(fastapi_app)["components"]["schemas"]["SessionEndAuditParams"]
    assert set(params["required"]) == {"username", "cause"}
    assert params["properties"]["session_count"] == {"type": "integer", "title": "Session Count"}
