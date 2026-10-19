"""Corps and brigades on posts: every post shows its units (from the tree, current names); posts and users filter by
corps_id / brigade_id (posts under the unit now; users by home post)."""
from app.backend.api.v1 import V1
from app.backend.auth.scope import Node
from app.backend.enums.general import Permissions
from app.backend.enums.routes import PostRoutes, ScopeGroupRoutes, SearchRoutes, UserRoutes
from tests.helpers import assign, make_admin, make_group, make_post, make_role, make_user

POSTS = V1.path(PostRoutes.ROOT)


def _post(post_id: int) -> str:
    return V1.path(PostRoutes.POST_BY_ID, post_id=post_id)


async def _structure(db):
    """corps 1 > brigade 10 > post A; corps 1 > post B; plain group > post C; post D at the root"""
    corps = await make_group(db, name="Corps 1", kind="corps")
    brigade = await make_group(db, name="Brigade 10", parent_id=corps.id, kind="brigade")
    plain = await make_group(db, name="Sector")
    a = await make_post(db, group_id=brigade.id)
    b = await make_post(db, group_id=corps.id)
    c = await make_post(db, group_id=plain.id)
    d = await make_post(db)
    return corps, brigade, plain, (a, b, c, d)


def _ref(group):
    return {"id": group.id, "name": group.name}


async def test_post_shows_its_units_wherever_it_sits(client, db, headers):
    h = headers(await make_admin(db))
    corps, brigade, _, (a, b, c, d) = await _structure(db)
    shown = {p["id"]: p for p in (await client.get(POSTS, headers=h)).json()}
    assert (shown[a.id]["corps"], shown[a.id]["brigade"]) == (_ref(corps), _ref(brigade))
    assert (shown[b.id]["corps"], shown[b.id]["brigade"]) == (_ref(corps), None)
    assert (shown[c.id]["corps"], shown[c.id]["brigade"]) == (None, None)
    assert (shown[d.id]["corps"], shown[d.id]["brigade"]) == (None, None)


async def test_units_follow_moves_of_the_post_and_of_its_brigade(client, db, headers):
    h = headers(await make_admin(db))
    corps, brigade, _, (a, b, _, _) = await _structure(db)
    other = await make_group(db, name="Corps 2", kind="corps")

    r = await client.put(_post(b.id), json={"group_id": brigade.id}, headers=h)
    assert r.status_code == 200, r.text
    assert (r.json()["corps"], r.json()["brigade"]) == (_ref(corps), _ref(brigade))

    r = await client.patch(V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=brigade.id), json={"parent_id": other.id},
                           headers=h)
    assert r.status_code == 200, r.text
    one = (await client.get(_post(a.id), headers=h)).json()
    assert (one["corps"], one["brigade"]) == (_ref(other), _ref(brigade))

    r = await client.patch(V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=other.id), json={"name": "Corps 2 renamed"},
                           headers=h)
    assert r.status_code == 200
    one = (await client.get(_post(a.id), headers=h)).json()
    assert one["corps"]["name"] == "Corps 2 renamed"


async def test_every_post_operation_fills_the_units(client, db, headers):
    h = headers(await make_admin(db))
    corps, brigade, _, (a, _, _, _) = await _structure(db)
    expected = (_ref(corps), _ref(brigade))

    created = await client.post(POSTS, json={"name": "New", "code": "NEW-1", "group_id": brigade.id}, headers=h)
    assert created.status_code == 200, created.text
    assert (created.json()["corps"], created.json()["brigade"]) == expected
    new_id = created.json()["id"]

    answers = [
        (await client.get(_post(a.id), headers=h)).json(),
        (await client.put(_post(a.id), json={"name": "Renamed"}, headers=h)).json(),
        (await client.post(V1.path(PostRoutes.POST_ARCHIVE, post_id=new_id), headers=h)).json(),
        (await client.post(V1.path(PostRoutes.POST_RESTORE, post_id=new_id), headers=h)).json(),
    ]
    for answer in answers:
        assert (answer["corps"], answer["brigade"]) == expected, answer
    found = (await client.get(V1.path(SearchRoutes.POSTS), params={"id": a.id}, headers=h)).json()
    assert (found["items"][0]["corps"], found["items"][0]["brigade"]) == expected


async def test_posts_filter_by_corps_and_brigade(client, db, headers):
    h = headers(await make_admin(db))
    corps, brigade, plain, (a, b, c, d) = await _structure(db)

    async def ids(params):
        r = await client.get(POSTS, params=params, headers=h)
        assert r.status_code == 200, r.text
        listed = {p["id"] for p in r.json()}
        found = await client.get(V1.path(SearchRoutes.POSTS), params=params, headers=h)
        assert found.status_code == 200, found.text
        assert {p["id"] for p in found.json()["items"]} == listed and found.json()["total"] == len(listed)
        return listed

    assert await ids({"corps_id": corps.id}) == {a.id, b.id}
    assert await ids({"brigade_id": brigade.id}) == {a.id}
    assert await ids({"corps_id": corps.id, "brigade_id": brigade.id}) == {a.id}
    # an id of another kind, a plain group, an unknown id: nothing (never an error)
    assert await ids({"brigade_id": corps.id}) == set()
    assert await ids({"corps_id": brigade.id}) == set()
    assert await ids({"corps_id": plain.id}) == set()
    assert await ids({"corps_id": 99999}) == set()


async def test_unit_filter_narrows_access_never_widens_it(client, db, headers):
    corps, brigade, _, (a, b, _, _) = await _structure(db)
    viewer = await make_user(db)
    await assign(db, viewer, await make_role(db, [Permissions.POST_VIEW]), Node.post(b.id))
    r = await client.get(POSTS, params={"corps_id": corps.id}, headers=headers(viewer))
    assert r.status_code == 200 and [p["id"] for p in r.json()] == [b.id]


async def test_users_filter_by_home_post_unit(client, db, headers):
    admin = await make_admin(db)
    h = headers(admin)
    corps, brigade, _, (a, b, c, _) = await _structure(db)
    in_brigade = await make_user(db, post_id=a.id)
    in_corps = await make_user(db, post_id=b.id)
    outside = await make_user(db, post_id=c.id)

    async def ids(params):
        r = await client.get(V1.path(UserRoutes.ROOT), params=params, headers=h)
        assert r.status_code == 200, r.text
        listed = {u["id"] for u in r.json()}
        found = await client.get(V1.path(SearchRoutes.USERS), params=params, headers=h)
        assert found.status_code == 200, found.text
        assert {u["id"] for u in found.json()["items"]} == listed and found.json()["total"] == len(listed)
        return listed

    # the caller (no home post) is not listed by a unit filter
    assert await ids({"corps_id": corps.id}) == {in_brigade.id, in_corps.id}
    assert await ids({"brigade_id": brigade.id}) == {in_brigade.id}
    assert await ids({"brigade_id": corps.id}) == set()
    assert outside.id in await ids({})
