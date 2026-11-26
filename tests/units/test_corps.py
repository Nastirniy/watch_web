"""/corps: CRUD of corps (scope groups of kind corps under the root), the brigades and the posts of a corps."""
import pytest
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.auth.scope import Node, ROOT
from app.backend.enums.general import Permissions
from app.backend.enums.routes import CorpsRoutes
from app.backend.models.audit import AuditLog
from app.backend.models.scope_group import ScopeGroup
from tests.helpers import assign, make_admin, make_group, make_post, make_role, make_user

CORPS = V1.path(CorpsRoutes.ROOT)


def _one(corps_id: int) -> str:
    return V1.path(CorpsRoutes.CORPS_BY_ID, corps_id=corps_id)


async def _create(client, h, name, expect=201, **fields):
    r = await client.post(CORPS, json={"name": name, **fields}, headers=h)
    assert r.status_code == expect, r.text
    return r.json()


async def test_create_corps(client, db, headers):
    h = headers(await make_admin(db))
    corps = await _create(client, h, "Corps 1", description="First")
    assert corps["name"] == "Corps 1" and corps["description"] == "First"
    assert corps["brigade_count"] == 0 and corps["post_count"] == 0 and corps["is_deleted"] is False
    row = (await db.exec(select(ScopeGroup).where(ScopeGroup.id == corps["id"]))).one()
    assert row.kind == "corps" and row.parent_id is None
    audit = (await db.exec(select(AuditLog).where(AuditLog.resource_type == "scope_group"))).all()
    assert [(a.action, a.resource_id) for a in audit] == [("create", corps["id"])]


async def test_corps_body_refuses_kind_and_parent(client, db, headers):
    h = headers(await make_admin(db))
    for extra in ({"kind": "brigade"}, {"parent_id": 1}):
        r = await client.post(CORPS, json={"name": "Corps 1", **extra}, headers=h)
        assert r.status_code == 422 and r.json()["errors"][0]["code"] == "extra_forbidden"
    corps = await _create(client, h, "Corps 1")
    r = await client.patch(_one(corps["id"]), json={"kind": "group"}, headers=h)
    assert r.status_code == 422 and r.json()["errors"][0]["code"] == "extra_forbidden"


async def test_corps_name_taken_case_insensitive_and_against_root_groups(client, db, headers):
    h = headers(await make_admin(db))
    await _create(client, h, "Corps 1")
    r = await client.post(CORPS, json={"name": "corps 1"}, headers=h)
    assert r.status_code == 409 and r.json()["code"] == "scope_group_name_taken"
    await make_group(db, name="East")
    r = await client.post(CORPS, json={"name": "EAST"}, headers=h)
    assert r.status_code == 409 and r.json()["code"] == "scope_group_name_taken"
    other = await _create(client, h, "Corps 2")
    r = await client.patch(_one(other["id"]), json={"name": "CORPS 1"}, headers=h)
    assert r.status_code == 409 and r.json()["code"] == "scope_group_name_taken"


@pytest.mark.fit_types
async def test_create_needs_scope_group_create_at_the_root(client, db, headers):
    corps = await make_group(db, kind="corps")
    user = await make_user(db)
    role = await make_role(db, [Permissions.SCOPE_GROUP_VIEW, Permissions.SCOPE_GROUP_CREATE])
    await assign(db, user, role, Node.group(corps.id))
    r = await client.post(CORPS, json={"name": "Corps 9"}, headers=headers(user))
    assert r.status_code == 403 and r.json()["code"] == "permission_required_for_action"


async def test_get_and_update(client, db, headers):
    h = headers(await make_admin(db))
    corps = await _create(client, h, "Corps 1")
    r = await client.get(_one(corps["id"]), headers=h)
    assert r.status_code == 200 and r.json()["name"] == "Corps 1"
    r = await client.patch(_one(corps["id"]), json={"name": "Corps One", "description": "x"}, headers=h)
    assert r.status_code == 200 and (r.json()["name"], r.json()["description"]) == ("Corps One", "x")
    r = await client.patch(_one(corps["id"]), json={"name": None}, headers=h)
    assert r.status_code == 422
    audit = (await db.exec(select(AuditLog.action).where(AuditLog.resource_id == corps["id"],
                                                         AuditLog.resource_type == "scope_group"))).all()
    assert list(audit) == ["create", "update"]


@pytest.mark.fit_types
async def test_other_kinds_and_out_of_scope_answer_404(client, db, headers):
    h = headers(await make_admin(db))
    corps = await make_group(db, kind="corps")
    brigade = await make_group(db, parent_id=corps.id, kind="brigade")
    plain = await make_group(db)
    archived = await make_group(db, kind="corps")
    archived.is_deleted = True
    db.add(archived)
    await db.commit()
    for group_id in (brigade.id, plain.id, archived.id, 99999):
        for r in (await client.get(_one(group_id), headers=h),
                  await client.patch(_one(group_id), json={"name": "x"}, headers=h),
                  await client.get(V1.path(CorpsRoutes.CORPS_BRIGADES, corps_id=group_id), headers=h),
                  await client.get(V1.path(CorpsRoutes.CORPS_POSTS, corps_id=group_id), headers=h)):
            assert r.status_code == 404 and r.json()["code"] == "scope_group_not_found", (group_id, r.text)

    other = await make_group(db, kind="corps")
    user = await make_user(db)
    await assign(db, user, await make_role(db, [Permissions.SCOPE_GROUP_VIEW]), Node.group(corps.id))
    r = await client.get(_one(other.id), headers=headers(user))
    assert r.status_code == 404 and r.json()["code"] == "scope_group_not_found"
    listed = (await client.get(CORPS, headers=headers(user))).json()
    assert [c["id"] for c in listed] == [corps.id]


async def test_list_filters_and_sort(client, db, headers):
    h = headers(await make_admin(db))
    a = await _create(client, h, "Bravo")
    b = await _create(client, h, "Alpha")
    await make_group(db, name="Plain")
    archived = await make_group(db, name="Archived corps", kind="corps")
    archived.is_deleted = True
    db.add(archived)
    await db.commit()

    async def ids(params):
        r = await client.get(CORPS, params=params, headers=h)
        assert r.status_code == 200, r.text
        return [c["id"] for c in r.json()]

    assert await ids({"sort_by": "name"}) == [b["id"], a["id"]]
    assert await ids({"name_contains": "rav"}) == [a["id"]]
    assert await ids({"id": b["id"]}) == [b["id"]]
    assert set(await ids({"include_deleted": "true"})) == {a["id"], b["id"], archived.id}
    r = await client.get(CORPS, params={"sort_by": "kind"}, headers=h)
    assert r.status_code == 422


async def test_brigades_of_a_corps(client, db, headers):
    h = headers(await make_admin(db))
    corps = await make_group(db, kind="corps")
    other = await make_group(db, kind="corps")
    b1 = await make_group(db, name="Brigade 1", parent_id=corps.id, kind="brigade")
    b2 = await make_group(db, name="Brigade 2", parent_id=corps.id, kind="brigade")
    await make_group(db, name="Brigade 3", parent_id=other.id, kind="brigade")
    b4 = await make_group(db, name="Brigade 4", parent_id=corps.id, kind="brigade")
    b4.is_deleted = True
    db.add(b4)
    await db.commit()
    path = V1.path(CorpsRoutes.CORPS_BRIGADES, corps_id=corps.id)

    r = await client.get(path, params={"sort_by": "name", "sort_order": "descending"}, headers=h)
    assert r.status_code == 200, r.text
    assert [b["id"] for b in r.json()] == [b2.id, b1.id]
    assert r.json()[0]["corps_id"] == corps.id
    r = await client.get(path, params={"name_contains": "1"}, headers=h)
    assert [b["id"] for b in r.json()] == [b1.id]
    r = await client.get(path, params={"include_deleted": "true"}, headers=h)
    assert {b["id"] for b in r.json()} == {b1.id, b2.id, b4.id}


@pytest.mark.fit_types
async def test_posts_of_a_corps_and_counts(client, db, headers):
    corps = await make_group(db, kind="corps")
    brigade = await make_group(db, parent_id=corps.id, kind="brigade")
    hidden_brigade = await make_group(db, parent_id=corps.id, kind="brigade")
    in_brigade = await make_post(db, group_id=brigade.id)
    direct = await make_post(db, group_id=corps.id)
    elsewhere = await make_post(db, group_id=hidden_brigade.id)
    await make_post(db)
    archived = await make_post(db, group_id=corps.id)
    archived.is_deleted = True
    db.add(archived)
    await db.commit()

    h = headers(await make_admin(db))
    path = V1.path(CorpsRoutes.CORPS_POSTS, corps_id=corps.id)
    r = await client.get(path, headers=h)
    assert r.status_code == 200, r.text
    assert {p["id"] for p in r.json()} == {in_brigade.id, direct.id, elsewhere.id}
    r = await client.get(path, params={"id": direct.id}, headers=h)
    assert [p["id"] for p in r.json()] == [direct.id]
    shown = (await client.get(_one(corps.id), headers=h)).json()
    assert (shown["brigade_count"], shown["post_count"]) == (2, 3)

    # a viewer with scope_group.view at one brigade and post.view at two posts sees only those
    viewer = await make_user(db)
    await assign(db, viewer, await make_role(db, [Permissions.SCOPE_GROUP_VIEW]), ROOT)
    await assign(db, viewer, await make_role(db, [Permissions.POST_VIEW]), Node.post(in_brigade.id))
    await assign(db, viewer, await make_role(db, [Permissions.POST_VIEW]), Node.post(direct.id))
    r = await client.get(path, headers=headers(viewer))
    assert r.status_code == 200 and {p["id"] for p in r.json()} == {in_brigade.id, direct.id}
    shown = (await client.get(_one(corps.id), headers=headers(viewer))).json()
    assert (shown["brigade_count"], shown["post_count"]) == (2, 2)
