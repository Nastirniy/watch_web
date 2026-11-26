"""Observations record their units at create (observation_units): one row per unit above the post then, never changed
after; answers show the recorded units with today's names."""
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.auth import context
from app.backend.auth.context import load_scope_tree
from app.backend.enums.routes import BrigadeRoutes, ObservationRoutes, PostRoutes, ScopeGroupRoutes, SearchRoutes
from app.backend.models.observation import Observation
from app.backend.models.observation_unit import ObservationUnit
from app.backend.services.units import current_units_of_group
from tests.helpers import make_admin, make_dictionary, make_group, make_post, observation_payload

CREATE = V1.path(ObservationRoutes.CREATE)


async def _create(client, h, post_id, signal_type_id):
    r = await client.post(CREATE, json=observation_payload(post_id, signal_type_id), headers=h)
    assert r.status_code == 200, r.text
    return r.json()


async def _units(db, observation_id):
    rows = (await db.exec(select(ObservationUnit).where(ObservationUnit.observation_id == observation_id)
                          .order_by(ObservationUnit.group_id))).all()
    return [(row.group_id, row.created_at) for row in rows]


async def test_rows_written_at_create(client, db, headers):
    h = headers(await make_admin(db))
    ids = await make_dictionary(db)
    corps = await make_group(db, kind="corps")
    brigade = await make_group(db, parent_id=corps.id, kind="brigade")
    plain = await make_group(db)
    in_brigade = await _create(client, h, (await make_post(db, group_id=brigade.id)).id, ids["signal_type_id"])
    in_corps = await _create(client, h, (await make_post(db, group_id=corps.id)).id, ids["signal_type_id"])
    in_plain = await _create(client, h, (await make_post(db, group_id=plain.id)).id, ids["signal_type_id"])
    at_root = await _create(client, h, (await make_post(db)).id, ids["signal_type_id"])

    rows = await _units(db, in_brigade["id"])
    assert [group_id for group_id, _ in rows] == [corps.id, brigade.id]
    created = (await db.exec(select(Observation.created_at).where(Observation.id == in_brigade["id"]))).one()
    assert all(at == created for _, at in rows)
    assert [group_id for group_id, _ in await _units(db, in_corps["id"])] == [corps.id]
    assert await _units(db, in_plain["id"]) == []
    assert await _units(db, at_root["id"]) == []


async def test_recorded_units_survive_moves_and_show_current_names(client, db, headers):
    h = headers(await make_admin(db))
    ids = await make_dictionary(db)
    corps = await make_group(db, name="Corps 1", kind="corps")
    other = await make_group(db, name="Corps 2", kind="corps")
    brigade = await make_group(db, name="Brigade 10", parent_id=corps.id, kind="brigade")
    post = await make_post(db, group_id=brigade.id)
    created = await _create(client, h, post.id, ids["signal_type_id"])
    assert created["corps"] == {"id": corps.id, "name": "Corps 1"}
    assert created["brigade"] == {"id": brigade.id, "name": "Brigade 10"}
    before = await _units(db, created["id"])

    r = await client.put(V1.path(PostRoutes.POST_BY_ID, post_id=post.id), json={"group_id": other.id}, headers=h)
    assert r.status_code == 200, r.text
    r = await client.patch(V1.path(BrigadeRoutes.BRIGADE_BY_ID, brigade_id=brigade.id), json={"corps_id": other.id},
                           headers=h)
    assert r.status_code == 200, r.text
    r = await client.patch(V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=brigade.id), json={"name": "Brigade 10a"},
                           headers=h)
    assert r.status_code == 200, r.text
    assert await _units(db, created["id"]) == before

    listed = (await client.get(V1.path(ObservationRoutes.LIST_ALL), headers=h)).json()
    shown = next(o for o in listed if o["id"] == created["id"])
    assert shown["corps"] == {"id": corps.id, "name": "Corps 1"}
    assert shown["brigade"] == {"id": brigade.id, "name": "Brigade 10a"}
    found = (await client.get(V1.path(SearchRoutes.OBSERVATIONS), headers=h)).json()["items"]
    assert next(o for o in found if o["id"] == created["id"])["brigade"]["name"] == "Brigade 10a"
    archived = await client.post(V1.path(ObservationRoutes.OBSERVATION_ARCHIVE, observation_id=created["id"]), headers=h)
    assert archived.status_code == 200 and archived.json()["corps"]["id"] == corps.id
    assert await _units(db, created["id"]) == before


async def test_observation_outside_any_unit_has_none(client, db, headers):
    h = headers(await make_admin(db))
    ids = await make_dictionary(db)
    created = await _create(client, h, (await make_post(db)).id, ids["signal_type_id"])
    assert created["corps"] is None and created["brigade"] is None


async def test_create_records_a_brigade_the_request_tree_does_not_know(client, db, headers, monkeypatch):
    """The tree of the request was loaded before a brigade was added and the post moved into it: the rows follow the
    database"""
    h = headers(await make_admin(db))
    ids = await make_dictionary(db)
    corps = await make_group(db, kind="corps")
    post = await make_post(db, group_id=corps.id)
    stale = await load_scope_tree(db)

    brigade = await make_group(db, parent_id=corps.id, kind="brigade")
    post.group_id = brigade.id
    await db.commit()
    assert stale.units_of_group(post.group_id) == {}        # the old tree has never heard of the brigade

    async def old_tree(session):
        return stale
    monkeypatch.setattr(context, "load_scope_tree", old_tree)
    created = await _create(client, h, post.id, ids["signal_type_id"])
    assert [group_id for group_id, _ in await _units(db, created["id"])] == [corps.id, brigade.id]
    assert created["corps"]["id"] == corps.id and created["brigade"]["id"] == brigade.id


async def test_current_units_of_group_equal_the_tree(db):
    corps = await make_group(db, kind="corps")
    plain = await make_group(db, parent_id=corps.id)
    brigade = await make_group(db, parent_id=plain.id, kind="brigade")
    archived_corps = await make_group(db, kind="corps")
    archived_brigade = await make_group(db, parent_id=archived_corps.id, kind="brigade")
    archived_corps.is_deleted = True
    archived_brigade.is_deleted = True
    await db.commit()
    lone = await make_group(db)
    tree = await load_scope_tree(db)

    for group in (corps, plain, brigade, archived_corps, archived_brigade, lone):
        assert await current_units_of_group(db, group.id) == tree.units_of_group(group.id)
    assert await current_units_of_group(db, brigade.id) == {"corps": corps.id, "brigade": brigade.id}
    assert await current_units_of_group(db, plain.id) == {"corps": corps.id}
    assert await current_units_of_group(db, archived_brigade.id) == {"corps": archived_corps.id,
                                                                    "brigade": archived_brigade.id}
    assert await current_units_of_group(db, lone.id) == {}
    assert await current_units_of_group(db, None) == {}
    assert await current_units_of_group(db, 999999) == tree.units_of_group(999999) == {}
