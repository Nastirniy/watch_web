"""Every user record shows its home node and the units around it (home, post, brigade, corps): from the scope tree,
current names, in a constant number of queries for a page."""
from sqlalchemy import event

from app.backend.api.v1 import V1
from app.backend.auth.scope import Node
from app.backend.enums.general import BuiltinRole, Permissions, UserType
from app.backend.enums.routes import AuthRoutes, ScopeGroupRoutes, SearchRoutes, UserRoutes
from tests.helpers import assign, builtin_role, make_admin, make_group, make_post, make_role, make_user

USERS = V1.path(UserRoutes.ROOT)


def _user(user_id: int) -> str:
    return V1.path(UserRoutes.USER_BY_ID, user_id=user_id)


async def _structure(db):
    """corps C > brigade B > posts P1, P2; corps C > post P0; plain group S > post P3; post P4 at the root"""
    corps = await make_group(db, name="Corps C", kind="corps")
    brigade = await make_group(db, name="Brigade B", parent_id=corps.id, kind="brigade")
    plain = await make_group(db, name="Sector S")
    posts = [
        await make_post(db, group_id=brigade.id),
        await make_post(db, group_id=brigade.id),
        await make_post(db, group_id=corps.id),
        await make_post(db, group_id=plain.id),
        await make_post(db),
    ]
    return corps, brigade, posts


def _ref(group):
    return {"id": group.id, "name": group.name}


def _shown(post):
    return {"kind": "post", "id": post.id, "name": post.name, "group_kind": None}, \
        {"id": post.id, "name": post.name, "code": post.code}


def _units(record):
    return {key: record[key] for key in ("home", "post", "brigade", "corps")}


async def test_user_record_carries_home_post_and_its_units(client, db, headers):
    h = headers(await make_admin(db))
    corps, brigade, (p1, p2, p0, p3, p4) = await _structure(db)
    at_p1 = await make_user(db, post_id=p1.id)
    at_p0 = await make_user(db, post_id=p0.id)
    at_p3 = await make_user(db, post_id=p3.id)
    at_p4 = await make_user(db, post_id=p4.id)
    homeless = await make_user(db)

    home, post = _shown(p1)
    expected = {
        at_p1.id: {"home": home, "post": post, "brigade": _ref(brigade), "corps": _ref(corps)},
        at_p0.id: {"home": _shown(p0)[0], "post": _shown(p0)[1], "brigade": None, "corps": _ref(corps)},
        at_p3.id: {"home": _shown(p3)[0], "post": _shown(p3)[1], "brigade": None, "corps": None},
        at_p4.id: {"home": _shown(p4)[0], "post": _shown(p4)[1], "brigade": None, "corps": None},
        homeless.id: {"home": None, "post": None, "brigade": None, "corps": None},
    }

    for user_id, units in expected.items():
        one = await client.get(_user(user_id), headers=h)
        assert one.status_code == 200, one.text
        assert _units(one.json()) == units

    listed = {u["id"]: u for u in (await client.get(USERS, params={"page_size": 100}, headers=h)).json()}
    searched = {u["id"]: u for u in
                (await client.get(V1.path(SearchRoutes.USERS), params={"page_size": 100}, headers=h)).json()["items"]}
    for user_id, units in expected.items():
        assert _units(listed[user_id]) == units
        assert _units(searched[user_id]) == units


async def test_units_follow_a_brigade_move_and_a_rename(client, db, headers):
    h = headers(await make_admin(db))
    corps, brigade, (p1, *_) = await _structure(db)
    other = await make_group(db, name="Corps D", kind="corps")
    user = await make_user(db, post_id=p1.id)

    group_path = V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=brigade.id)
    r = await client.patch(group_path, json={"parent_id": other.id}, headers=h)
    assert r.status_code == 200, r.text
    assert (await client.get(_user(user.id), headers=h)).json()["corps"] == _ref(other)

    r = await client.patch(group_path, json={"name": "Brigade renamed"}, headers=h)
    assert r.status_code == 200, r.text
    one = (await client.get(_user(user.id), headers=h)).json()
    assert one["brigade"] == {"id": brigade.id, "name": "Brigade renamed"}
    assert one["corps"] == _ref(other)


async def test_auth_me_carries_the_units(client, db, headers):
    corps, brigade, (p1, *_) = await _structure(db)
    user = await make_user(db, post_id=p1.id)
    me = await client.get(V1.path(AuthRoutes.ME), headers=headers(user))
    assert me.status_code == 200, me.text
    home, post = _shown(p1)
    assert me.json()["post_id"] == p1.id
    assert _units(me.json()) == {"home": home, "post": post, "brigade": _ref(brigade), "corps": _ref(corps)}


async def test_a_page_of_users_reads_unit_names_in_constant_queries(client, db, headers, engine):
    h = headers(await make_admin(db))
    corps = [await make_group(db, kind="corps") for _ in range(2)]
    brigades = [await make_group(db, parent_id=corps[0].id, kind="brigade"),
                await make_group(db, parent_id=corps[0].id, kind="brigade"),
                await make_group(db, parent_id=corps[1].id, kind="brigade")]
    posts = [await make_post(db, group_id=brigades[i % 3].id) for i in range(5)]

    async def add_users(count):
        for i in range(count):
            await make_user(db, post_id=posts[i % 5].id)

    statements = []

    def count(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", count)
    try:
        await add_users(30)
        await client.get(USERS, params={"page_size": 100}, headers=h)   # the first request touches the session
        statements.clear()
        first = await client.get(USERS, params={"page_size": 100}, headers=h)
        assert first.status_code == 200, first.text
        small = len(statements)
        await add_users(30)
        statements.clear()
        second = await client.get(USERS, params={"page_size": 100}, headers=h)
        assert second.status_code == 200, second.text
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", count)
    assert len(second.json()) > len(first.json())
    assert all(u["corps"] is not None for u in second.json() if u["post_id"] is not None)
    assert len(statements) == small


async def _membership(db):
    """The tree of _structure plus an archived post P5 under B, users homed at P0, P1, P2 and assigned-only users
    (home P4, outside the unit): A1 at P1, A2 at B, A3 at P1 and P2, A4 at the archived P5, A5 at P1 through an
    archived role"""
    corps, brigade, (p1, p2, p0, p3, p4) = await _structure(db)
    p5 = await make_post(db, group_id=brigade.id)
    p5.is_deleted = True
    db.add(p5)
    await db.commit()
    role = await make_role(db, [Permissions.OBSERVATION_VIEW])
    dead_role = await make_role(db, [Permissions.OBSERVATION_VIEW])
    dead_role.is_deleted = True
    db.add(dead_role)
    await db.commit()

    home = {n: await make_user(db, post_id=post.id) for n, post in (("p0", p0), ("p1", p1), ("p2", p2))}
    a1, a2, a3, a4, a5 = [await make_user(db, post_id=p4.id) for _ in range(5)]
    await assign(db, a1, role, Node.post(p1.id))
    await assign(db, a2, role, Node.group(brigade.id))
    await assign(db, a3, role, Node.post(p1.id))
    await assign(db, a3, role, Node.post(p2.id))
    await assign(db, a4, role, Node.post(p5.id))
    await assign(db, a5, dead_role, Node.post(p1.id))
    return corps, brigade, (p1, p2, p0, p3, p4, p5), home, (a1, a2, a3, a4, a5)


async def _ids(client, h, **params):
    response = await client.get(USERS, params={"page_size": 100, **params}, headers=h)
    assert response.status_code == 200, response.text
    return [u["id"] for u in response.json()]


async def test_brigade_filter_lists_home_and_assigned_users_once(client, db, headers):
    h = headers(await make_admin(db))
    corps, brigade, posts, home, (a1, a2, a3, a4, a5) = await _membership(db)
    listed = await _ids(client, h, brigade_id=brigade.id)
    assert sorted(listed) == sorted([home["p1"].id, home["p2"].id, a1.id, a2.id, a3.id])


async def test_membership_home_and_assigned_split_the_set(client, db, headers):
    h = headers(await make_admin(db))
    corps, brigade, posts, home, (a1, a2, a3, a4, a5) = await _membership(db)
    assert sorted(await _ids(client, h, brigade_id=brigade.id, membership="home")) ==         sorted([home["p1"].id, home["p2"].id])
    assert sorted(await _ids(client, h, brigade_id=brigade.id, membership="assigned")) == sorted([a1.id, a2.id, a3.id])


async def test_corps_filter_reaches_posts_directly_under_the_corps(client, db, headers):
    h = headers(await make_admin(db))
    corps, brigade, posts, home, (a1, a2, a3, a4, a5) = await _membership(db)
    assert sorted(await _ids(client, h, corps_id=corps.id)) ==         sorted([home["p0"].id, home["p1"].id, home["p2"].id, a1.id, a2.id, a3.id])


async def test_post_filter_lists_assigned_users(client, db, headers):
    h = headers(await make_admin(db))
    corps, brigade, (p1, *_), home, (a1, a2, a3, a4, a5) = await _membership(db)
    assert sorted(await _ids(client, h, post_id=p1.id)) == sorted([home["p1"].id, a1.id, a3.id])
    assert await _ids(client, h, post_id=p1.id, membership="home") == [home["p1"].id]


async def test_unit_filter_never_widens_visibility(client, db, headers):
    corps, brigade, (p1, *_), home, (a1, *_rest) = await _membership(db)
    caller = await make_user(db, user_type=UserType.ADMINISTRATOR)
    await assign(db, caller, await builtin_role(db, BuiltinRole.ADMINISTRATOR), Node.post(p1.id))
    listed = await _ids(client, headers(caller), brigade_id=brigade.id)
    assert sorted(listed) == sorted([home["p1"].id, caller.id])
    assert a1.id not in listed


async def test_caller_outside_the_unit_is_not_listed(client, db, headers):
    caller = await make_admin(db)
    corps, brigade, *_ = await _membership(db)
    assert caller.id not in await _ids(client, headers(caller), brigade_id=brigade.id)


async def test_search_users_has_the_same_membership(client, db, headers):
    h = headers(await make_admin(db))
    corps, brigade, *_ = await _membership(db)
    listed = await _ids(client, h, brigade_id=brigade.id, membership="assigned")
    searched = await client.get(V1.path(SearchRoutes.USERS),
                                params={"page_size": 100, "brigade_id": brigade.id, "membership": "assigned"}, headers=h)
    assert searched.status_code == 200, searched.text
    assert sorted(u["id"] for u in searched.json()["items"]) == sorted(listed)
    assert searched.json()["total"] == len(listed) == 3


async def test_a_created_user_is_listed_once_because_its_base_role_sits_at_the_home_post(client, db, headers):
    h = headers(await make_admin(db))
    corps, brigade, (p1, *_rest) = await _structure(db)
    created = await client.post(USERS, headers=h, data={"username": "made_here", "username_alias": "Made here",
                                                        "user_type": "viewer", "post_id": str(p1.id)})
    assert created.status_code == 200, created.text
    user_id = created.json()["id"]
    # home P1 and the base role at P1: one row under any, and under both split sets
    assert (await _ids(client, h, brigade_id=brigade.id)).count(user_id) == 1
    assert user_id in await _ids(client, h, brigade_id=brigade.id, membership="home")
    assert user_id in await _ids(client, h, brigade_id=brigade.id, membership="assigned")


async def test_a_moved_user_keeps_its_base_role_at_the_old_post(client, db, headers):
    h = headers(await make_admin(db))
    corps, brigade, (p1, p2, p0, p3, p4) = await _structure(db)
    created = await client.post(USERS, headers=h, data={"username": "will_move", "username_alias": "Will move",
                                                        "user_type": "viewer", "post_id": str(p1.id)})
    assert created.status_code == 200, created.text
    user_id = created.json()["id"]
    moved = await client.patch(_user(user_id), headers=h, data={"post_id": p4.id})
    assert moved.status_code == 200, moved.text
    # the base role stays at P1: still a member there by assignment, no longer by home
    assert user_id in await _ids(client, h, post_id=p1.id)
    assert user_id in await _ids(client, h, post_id=p1.id, membership="assigned")
    assert user_id not in await _ids(client, h, post_id=p1.id, membership="home")
    assert user_id in await _ids(client, h, post_id=p4.id, membership="home")


async def test_an_assignment_at_an_archived_subgroup_does_not_count(client, db, headers):
    h = headers(await make_admin(db))
    corps, brigade, (p1, p2, p0, p3, p4) = await _structure(db)
    sub = await make_group(db, name="Archived sub", parent_id=brigade.id)
    sub.is_deleted = True
    db.add(sub)
    await db.commit()
    role = await make_role(db, [Permissions.OBSERVATION_VIEW])
    a6 = await make_user(db, post_id=p4.id)
    await assign(db, a6, role, Node.group(sub.id))
    assert a6.id not in await _ids(client, h, brigade_id=brigade.id)
    assert a6.id not in await _ids(client, h, brigade_id=brigade.id, membership="assigned")
