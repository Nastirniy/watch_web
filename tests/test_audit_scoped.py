"""The scoped view of the audit log (part 6, task 6): audit.view_scope (tree level) reads the rows of the posts where
it is in effect and of the groups inside the subtrees where it is held; rows at the root only with it held at the
root; rows without a scope (system rows) never. The device fields (ip_address, user_agent, session_id, request_url)
stay with audit.view. An entry that exists outside the caller's scope is 403 audit_entry_access_denied; a post_id /
group_id / corps_id / brigade_id filter outside it is 403."""
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.auth.builtin_roles import BUILTIN_ROLES
from app.backend.auth.scope import Node, ROOT
from app.backend.auth.user_types import USER_TYPES, boundary
from app.backend.bootstrap.permissions import PERMISSION_LEVELS, PERMISSION_META
from app.backend.enums.audit import AuditEvent
from app.backend.enums.general import BuiltinRole, PermissionLevel, Permissions, RoleKind, UserType
from app.backend.enums.routes import AuditRoutes
from app.backend.models.role import Permission
from tests.helpers import assign, builtin_role, make_admin, make_group, make_post, make_user
from tests.test_audit_read import add_row

ENTRIES = V1.path(AuditRoutes.ROOT)
EVENTS = V1.path(AuditRoutes.EVENTS)
DEVICE = ("ip_address", "user_agent", "session_id", "request_url")


def _entry(audit_id: int) -> str:
    return V1.path(AuditRoutes.ENTRY_BY_ID, audit_id=audit_id)


async def _administrator(db, node: Node):
    """A user of type administrator holding the built-in administrator role at node"""
    user = await make_user(db, user_type=UserType.ADMINISTRATOR)
    await assign(db, user, await builtin_role(db, BuiltinRole.ADMINISTRATOR), node)
    return user


def _group_row(group, kind: str):
    return AuditEvent.SCOPE_GROUP_UPDATE, {"group_name": group.name, "group_kind": kind}


async def _dataset(db):
    """corps C > brigade B > posts P1, P2; corps C > post P0; a row at each node, at the root and without a scope"""
    corps = await make_group(db, name="Corps C", kind="corps")
    brigade = await make_group(db, name="Brigade B", parent_id=corps.id, kind="brigade")
    p1 = await make_post(db, group_id=brigade.id)
    p2 = await make_post(db, group_id=brigade.id)
    p0 = await make_post(db, group_id=corps.id)

    async def post_row(post, **columns):
        return await add_row(db, AuditEvent.POST_UPDATE, {"post_name": post.name, "post_code": post.code},
                             scope=Node.post(post.id), resource_id=post.id, **columns)

    rows = {
        "p1": await post_row(p1, ip_address="10.0.0.1", user_agent="agent", session_id="sess-p1",
                             request_url="http://test/p1", request_id="ab" * 16),
        "p2": await post_row(p2),
        "p0": await post_row(p0),
        "b": await add_row(db, *_group_row(brigade, "brigade"), scope=Node.group(brigade.id), resource_id=brigade.id),
        "c": await add_row(db, *_group_row(corps, "corps"), scope=Node.group(corps.id), resource_id=corps.id),
        "root": await add_row(db, AuditEvent.USER_UPDATE, {"username": "nobody"}, scope=ROOT),
        "system": await add_row(db, AuditEvent.TAG_CREATE, {"tag_name": "Red"}, resource_id=77),
    }
    return {"corps": corps, "brigade": brigade, "p1": p1, "p2": p2, "p0": p0, "rows": rows}


async def _names(client, h, data, **params) -> set:
    response = await client.get(ENTRIES, params={"page_size": 200, **params}, headers=h)
    assert response.status_code == 200, response.text
    names = {row.id: name for name, row in data["rows"].items()}
    return {names[entry["id"]] for entry in response.json()}


# ---------------------------------------------------------------- what is visible

async def test_brigade_admin_sees_rows_of_its_posts_and_groups(client, db, headers):
    data = await _dataset(db)
    h = headers(await _administrator(db, Node.group(data["brigade"].id)))
    assert await _names(client, h, data) == {"p1", "p2", "b"}
    # the catalog is readable with the scoped view too
    assert (await client.get(EVENTS, headers=h)).status_code == 200


async def test_root_grant_sees_every_scoped_row_but_no_system_row(client, db, headers):
    data = await _dataset(db)
    h = headers(await _administrator(db, ROOT))
    assert await _names(client, h, data) == {"p1", "p2", "p0", "b", "c", "root"}
    denied = await client.get(_entry(data["rows"]["system"].id), headers=h)
    assert denied.status_code == 403 and denied.json()["code"] == "audit_entry_access_denied"


async def test_system_view_still_sees_every_row(client, db, headers):
    data = await _dataset(db)
    h = headers(await make_admin(db))
    assert await _names(client, h, data) == set(data["rows"])


async def test_device_fields_are_null_without_audit_view(client, db, headers):
    data = await _dataset(db)
    h = headers(await _administrator(db, Node.group(data["brigade"].id)))
    row = data["rows"]["p1"]
    listed = {e["id"]: e for e in (await client.get(ENTRIES, headers=h)).json()}
    one = (await client.get(_entry(row.id), headers=h)).json()
    for entry in (listed[row.id], one):
        assert {key: entry[key] for key in DEVICE} == {key: None for key in DEVICE}
        assert entry["request_id"] == "ab" * 16      # not a device field
    # the session filter needs the device fields: it matches nothing
    assert await _names(client, h, data, session_id="sess-p1") == set()

    system = (await client.get(_entry(row.id), headers=headers(await make_admin(db)))).json()
    assert system["ip_address"] == "10.0.0.1" and system["session_id"] == "sess-p1"


async def test_entry_outside_scope_is_403(client, db, headers):
    data = await _dataset(db)
    h = headers(await _administrator(db, Node.group(data["brigade"].id)))
    assert (await client.get(_entry(data["rows"]["p1"].id), headers=h)).status_code == 200
    assert (await client.get(_entry(data["rows"]["b"].id), headers=h)).status_code == 200
    for name in ("p0", "c", "root", "system"):
        audit_id = data["rows"][name].id
        response = await client.get(_entry(audit_id), headers=h)
        assert response.status_code == 403, (name, response.text)
        body = response.json()
        assert body["code"] == "audit_entry_access_denied" and body["audit_id"] == audit_id
        assert body["required"] == {"all": [], "any": ["audit.view", "audit.view_scope"], "at_root": []}
        assert body["missing"] == ["audit.view", "audit.view_scope"]
    # a row that does not exist stays 404
    missing = await client.get(_entry(max(row.id for row in data["rows"].values()) + 1000), headers=h)
    assert missing.status_code == 404 and missing.json()["code"] == "audit_entry_not_found"


async def test_archived_post_rows_stay_visible_through_the_group_grant(client, db, headers):
    data = await _dataset(db)
    p2 = data["p2"]
    p2.is_deleted = True
    db.add(p2)
    await db.commit()
    h = headers(await _administrator(db, Node.group(data["brigade"].id)))
    assert await _names(client, h, data) == {"p1", "p2", "b"}
    assert (await client.get(_entry(data["rows"]["p2"].id), headers=h)).status_code == 200


async def test_a_grant_on_an_archived_post_applies_nowhere(client, db, headers):
    data = await _dataset(db)
    h = headers(await _administrator(db, Node.post(data["p2"].id)))
    assert await _names(client, h, data) == {"p2"}
    p2 = data["p2"]
    p2.is_deleted = True
    db.add(p2)
    await db.commit()
    response = await client.get(ENTRIES, headers=h)
    assert response.status_code == 403 and response.json()["code"] == "permission_denied"


# ---------------------------------------------------------------- filters

async def test_post_filter_outside_scope_is_403(client, db, headers):
    data = await _dataset(db)
    h = headers(await _administrator(db, Node.group(data["brigade"].id)))
    assert await _names(client, h, data, post_id=data["p1"].id) == {"p1"}
    response = await client.get(ENTRIES, params={"post_id": data["p0"].id}, headers=h)
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "post_access_denied"
    assert response.json()["post_id"] == data["p0"].id
    assert response.json()["missing"] == ["audit.view_scope"]


async def test_group_filters_outside_scope_are_403(client, db, headers):
    data = await _dataset(db)
    h = headers(await _administrator(db, Node.group(data["brigade"].id)))
    brigade, corps = data["brigade"].id, data["corps"].id
    assert await _names(client, h, data, group_id=brigade) == {"p1", "p2", "b"}
    assert await _names(client, h, data, brigade_id=brigade) == {"p1", "p2", "b"}
    for params in ({"group_id": corps}, {"corps_id": corps}, {"brigade_id": 987654}):
        response = await client.get(ENTRIES, params=params, headers=h)
        assert response.status_code == 403, (params, response.text)
        assert response.json()["code"] == "permission_required_for_action"
        assert response.json()["permission"] == "audit.view_scope"


async def test_filters_of_the_system_view_are_never_refused(client, db, headers):
    data = await _dataset(db)
    h = headers(await make_admin(db))
    assert await _names(client, h, data, post_id=data["p0"].id) == {"p0"}
    assert await _names(client, h, data, corps_id=data["corps"].id) == {"p1", "p2", "p0", "b", "c"}
    assert await _names(client, h, data, group_id=987654) == set()


# ---------------------------------------------------------------- the permission

async def test_new_permission_is_tree_level_and_in_administrator(db):
    name = Permissions.AUDIT_VIEW_SCOPE.value
    assert name == "audit.view_scope"
    assert PERMISSION_LEVELS[name] is PermissionLevel.TREE
    assert name in PERMISSION_META
    assert name in BUILTIN_ROLES[BuiltinRole.ADMINISTRATOR].permissions
    assert name not in BUILTIN_ROLES[BuiltinRole.AUDITOR].permissions
    holders = {user_type for user_type in USER_TYPES if name in boundary(user_type, RoleKind.TREE)}
    assert holders == {UserType.ADMINISTRATOR, UserType.SYSTEM_ADMINISTRATOR}
    assert all(name not in boundary(user_type, RoleKind.SYSTEM) for user_type in USER_TYPES)
    # seeding inserted the row with its level
    row = (await db.exec(select(Permission).where(Permission.name == name))).one()
    assert row.level == PermissionLevel.TREE.value and row.display_name
