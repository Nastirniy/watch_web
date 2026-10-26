"""Every user record shows its home node and the units around it (home, post, brigade, corps): from the scope tree,
current names, in a constant number of queries for a page."""
from sqlalchemy import event

from app.backend.api.v1 import V1
from app.backend.enums.routes import AuthRoutes, ScopeGroupRoutes, SearchRoutes, UserRoutes
from tests.helpers import make_admin, make_group, make_post, make_user

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
