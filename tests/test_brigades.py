"""/brigades: CRUD of brigades (scope groups of kind brigade inside a corps), moves between corps, posts of a brigade."""
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.auth.scope import Node, ROOT
from app.backend.enums.general import Permissions
from app.backend.enums.routes import BrigadeRoutes, PostRoutes
from app.backend.models.audit import AuditLog
from app.backend.models.scope_group import ScopeGroup
from tests.helpers import assign, make_admin, make_group, make_post, make_role, make_user

BRIGADES = V1.path(BrigadeRoutes.ROOT)


def _one(brigade_id: int) -> str:
    return V1.path(BrigadeRoutes.BRIGADE_BY_ID, brigade_id=brigade_id)


async def _archived(db, group):
    group.is_deleted = True
    db.add(group)
    await db.commit()
    return group


async def test_create_in_a_corps(client, db, headers):
    h = headers(await make_admin(db))
    corps = await make_group(db, kind="corps")
    r = await client.post(BRIGADES, json={"corps_id": corps.id, "name": "Brigade 10", "description": "d"}, headers=h)
    assert r.status_code == 201, r.text
    body = r.json()
    assert (body["corps_id"], body["name"], body["description"], body["post_count"]) == (corps.id, "Brigade 10", "d", 0)
    row = (await db.exec(select(ScopeGroup).where(ScopeGroup.id == body["id"]))).one()
    assert (row.kind, row.parent_id, row.parent_key) == ("brigade", corps.id, corps.id)
    audit = (await db.exec(select(AuditLog.action).where(AuditLog.resource_id == body["id"],
                                                         AuditLog.resource_type == "scope_group"))).all()
    assert list(audit) == ["create"]


async def test_create_outside_a_corps_is_refused(client, db, headers):
    h = headers(await make_admin(db))
    corps = await make_group(db, kind="corps")
    brigade = await make_group(db, parent_id=corps.id, kind="brigade")
    plain = await make_group(db)
    for parent in (brigade, plain):
        r = await client.post(BRIGADES, json={"corps_id": parent.id, "name": "B"}, headers=h)
        assert r.status_code == 409, r.text
        assert (r.json()["code"], r.json()["group_kind"], r.json()["parent_kind"]) == (
            "scope_group_parent_not_allowed", "brigade", parent.kind)
    for body in ({"name": "B"}, {"corps_id": None, "name": "B"}, {"corps_id": corps.id, "name": "B", "kind": "corps"}):
        r = await client.post(BRIGADES, json=body, headers=h)
        assert r.status_code == 422, r.text


async def test_create_in_an_archived_or_unknown_corps_is_404(client, db, headers):
    h = headers(await make_admin(db))
    corps = await _archived(db, await make_group(db, kind="corps"))
    for corps_id in (corps.id, 99999):
        r = await client.post(BRIGADES, json={"corps_id": corps_id, "name": "B"}, headers=h)
        assert r.status_code == 404 and r.json()["code"] == "scope_group_not_found"


async def test_create_needs_scope_group_create_covering_the_corps(client, db, headers):
    corps = await make_group(db, kind="corps")
    other = await make_group(db, kind="corps")
    user = await make_user(db)
    await assign(db, user, await make_role(db, [Permissions.SCOPE_GROUP_VIEW]), ROOT)
    await assign(db, user, await make_role(db, [Permissions.SCOPE_GROUP_CREATE]), Node.group(corps.id))
    r = await client.post(BRIGADES, json={"corps_id": corps.id, "name": "B"}, headers=headers(user))
    assert r.status_code == 201, r.text
    r = await client.post(BRIGADES, json={"corps_id": other.id, "name": "B"}, headers=headers(user))
    assert r.status_code == 403 and r.json()["code"] == "permission_required_for_action"


async def test_name_unique_within_one_corps_only(client, db, headers):
    h = headers(await make_admin(db))
    c1 = await make_group(db, kind="corps")
    c2 = await make_group(db, kind="corps")
    assert (await client.post(BRIGADES, json={"corps_id": c1.id, "name": "Brigade 10"}, headers=h)).status_code == 201
    assert (await client.post(BRIGADES, json={"corps_id": c2.id, "name": "Brigade 10"}, headers=h)).status_code == 201
    r = await client.post(BRIGADES, json={"corps_id": c1.id, "name": "BRIGADE 10"}, headers=h)
    assert r.status_code == 409 and r.json()["code"] == "scope_group_name_taken"


async def test_get_update_and_404s(client, db, headers):
    h = headers(await make_admin(db))
    corps = await make_group(db, kind="corps")
    brigade = await make_group(db, name="Brigade 10", parent_id=corps.id, kind="brigade")
    r = await client.get(_one(brigade.id), headers=h)
    assert r.status_code == 200 and r.json()["corps_id"] == corps.id
    r = await client.patch(_one(brigade.id), json={"name": "Brigade 11", "description": "x"}, headers=h)
    assert r.status_code == 200 and (r.json()["name"], r.json()["description"]) == ("Brigade 11", "x")
    archived = await _archived(db, await make_group(db, parent_id=corps.id, kind="brigade"))
    for group_id in (corps.id, (await make_group(db)).id, archived.id, 99999):
        for r in (await client.get(_one(group_id), headers=h),
                  await client.patch(_one(group_id), json={"name": "x"}, headers=h),
                  await client.get(V1.path(BrigadeRoutes.BRIGADE_POSTS, brigade_id=group_id), headers=h)):
            assert r.status_code == 404 and r.json()["code"] == "scope_group_not_found", (group_id, r.text)


async def test_move_takes_posts_along(client, db, headers):
    h = headers(await make_admin(db))
    c1 = await make_group(db, name="Corps 1", kind="corps")
    c2 = await make_group(db, name="Corps 2", kind="corps")
    brigade = await make_group(db, parent_id=c1.id, kind="brigade")
    post = await make_post(db, group_id=brigade.id)
    r = await client.patch(_one(brigade.id), json={"corps_id": c2.id}, headers=h)
    assert r.status_code == 200, r.text
    assert r.json()["corps_id"] == c2.id
    row = (await db.exec(select(ScopeGroup).where(ScopeGroup.id == brigade.id))).one()
    await db.refresh(row)
    assert (row.parent_id, row.parent_key) == (c2.id, c2.id)
    shown = (await client.get(V1.path(PostRoutes.POST_BY_ID, post_id=post.id), headers=h)).json()
    assert shown["corps"] == {"id": c2.id, "name": "Corps 2"}


async def test_move_refusals(client, db, headers):
    h = headers(await make_admin(db))
    c1 = await make_group(db, kind="corps")
    brigade = await make_group(db, name="Brigade 10", parent_id=c1.id, kind="brigade")
    other_brigade = await make_group(db, parent_id=c1.id, kind="brigade")
    plain = await make_group(db)
    archived = await _archived(db, await make_group(db, kind="corps"))
    for target, code in ((other_brigade.id, "scope_group_parent_not_allowed"), (plain.id, "scope_group_parent_not_allowed"),
                         (archived.id, "scope_group_not_found"), (99999, "scope_group_not_found")):
        r = await client.patch(_one(brigade.id), json={"corps_id": target}, headers=h)
        assert r.json().get("code") == code, (target, r.text)
    r = await client.patch(_one(brigade.id), json={"corps_id": None}, headers=h)
    assert r.status_code == 422
    c2 = await make_group(db, kind="corps")
    await make_group(db, name="brigade 10", parent_id=c2.id, kind="brigade")
    r = await client.patch(_one(brigade.id), json={"corps_id": c2.id}, headers=h)
    assert r.status_code == 409 and r.json()["code"] == "scope_group_name_taken"


async def test_move_without_post_update_at_the_new_corps_changes_nothing(client, db, headers):
    c1 = await make_group(db, kind="corps")
    c2 = await make_group(db, kind="corps")
    brigade = await make_group(db, parent_id=c1.id, kind="brigade")
    user = await make_user(db)
    await assign(db, user, await make_role(db, [Permissions.SCOPE_GROUP_VIEW, Permissions.SCOPE_GROUP_UPDATE]), ROOT)
    await assign(db, user, await make_role(db, [Permissions.POST_UPDATE]), Node.group(c1.id))
    r = await client.patch(_one(brigade.id), json={"corps_id": c2.id}, headers=headers(user))
    assert r.status_code == 403 and r.json()["code"] == "permission_required_for_action"
    row = (await db.exec(select(ScopeGroup).where(ScopeGroup.id == brigade.id))).one()
    await db.refresh(row)
    assert row.parent_id == c1.id


async def test_list_filters(client, db, headers):
    h = headers(await make_admin(db))
    c1 = await make_group(db, kind="corps")
    c2 = await make_group(db, kind="corps")
    b1 = await make_group(db, name="Alpha", parent_id=c1.id, kind="brigade")
    b2 = await make_group(db, name="Bravo", parent_id=c2.id, kind="brigade")
    await make_group(db, name="Plain")

    async def ids(params):
        r = await client.get(BRIGADES, params=params, headers=h)
        assert r.status_code == 200, r.text
        return [b["id"] for b in r.json()]

    assert await ids({"sort_by": "name"}) == [b1.id, b2.id]
    assert await ids({"corps_id": c2.id}) == [b2.id]
    assert await ids({"name_contains": "alp"}) == [b1.id]
    assert await ids({"corps_id": b1.id}) == []


async def test_posts_of_a_brigade(client, db, headers):
    h = headers(await make_admin(db))
    corps = await make_group(db, kind="corps")
    brigade = await make_group(db, parent_id=corps.id, kind="brigade")
    inside = await make_post(db, group_id=brigade.id)
    await make_post(db, group_id=corps.id)
    r = await client.get(V1.path(BrigadeRoutes.BRIGADE_POSTS, brigade_id=brigade.id), headers=h)
    assert r.status_code == 200 and [p["id"] for p in r.json()] == [inside.id]
    shown = (await client.get(_one(brigade.id), headers=h)).json()
    assert shown["post_count"] == 1
