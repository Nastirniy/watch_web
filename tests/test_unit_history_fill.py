"""fill_unit_history: old observations (no observation_units row of a kind) get the unit the post is in now."""
from typing import Dict, List

import pytest

from sqlalchemy import func
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.enums.routes import BrigadeRoutes, CorpsRoutes, ObservationRoutes, PostRoutes
from app.backend.models.audit import AuditLog
from app.backend.models.observation_unit import ObservationUnit
from app.backend.models.scope_group import ScopeGroup
from app.backend.cli.units import main, report_lines
from app.backend.services.unit_history import HistoryFill, UnitFill, fill_unit_history
from tests.helpers import make_admin, make_dictionary, make_group, make_post, observation_payload

CREATE = V1.path(ObservationRoutes.CREATE)
LIST_ALL = V1.path(ObservationRoutes.LIST_ALL)


async def _create(client, h, post_id, signal_type_id) -> int:
    r = await client.post(CREATE, json=observation_payload(post_id, signal_type_id), headers=h)
    assert r.status_code == 200, r.text
    return r.json()["id"]


async def _move(client, h, post_id, group_id):
    r = await client.put(V1.path(PostRoutes.POST_BY_ID, post_id=post_id), json={"group_id": group_id}, headers=h)
    assert r.status_code == 200, r.text


async def _old(client, h, ids, post, count) -> List[int]:
    """count observations created while the post sits where it is now (callers move it into a unit afterwards)"""
    return [await _create(client, h, post.id, ids["signal_type_id"]) for _ in range(count)]


async def _kinds(db, observation_id) -> Dict[str, int]:
    rows = await db.exec(select(ScopeGroup.kind, ScopeGroup.id)
                         .join(ObservationUnit, ObservationUnit.group_id == ScopeGroup.id)
                         .where(ObservationUnit.observation_id == observation_id))
    return {kind: group_id for kind, group_id in rows.all()}


async def _listed(client, h, **params) -> List[int]:
    r = await client.get(LIST_ALL, params={**params, "page_size": 200}, headers=h)
    assert r.status_code == 200, r.text
    data = r.json()
    return sorted(o["id"] for o in (data["items"] if isinstance(data, dict) else data))


async def _structure(db):
    k1 = await make_group(db, name="Corps 1", kind="corps")
    b1 = await make_group(db, name="Brigade 1", parent_id=k1.id, kind="brigade")
    return k1, b1


async def test_old_observations_get_the_post_units(client, db, headers):
    h = headers(await make_admin(db))
    ids = await make_dictionary(db)
    k1, b1 = await _structure(db)
    post = await make_post(db)
    old_ids = await _old(client, h, ids, post, 3)
    await _move(client, h, post.id, b1.id)
    assert [await _kinds(db, o) for o in old_ids] == [{}, {}, {}]

    result = await fill_unit_history(db)

    assert all([await _kinds(db, o) == {"corps": k1.id, "brigade": b1.id} for o in old_ids])
    assert result.observations == 3 and result.rows == 6 and not result.dry_run and result.posts_skipped == 0
    assert [(u.kind, u.group_id, u.name, u.observations) for u in result.units] == [
        ("brigade", b1.id, "Brigade 1", 3), ("corps", k1.id, "Corps 1", 3)]
    assert await _listed(client, h, corps_id=k1.id) == sorted(old_ids)
    assert await _listed(client, h, brigade_id=b1.id) == sorted(old_ids)
    stats = await client.get(V1.path(CorpsRoutes.CORPS_OBSERVATION_STATS, corps_id=k1.id), params={"tz": "UTC"},
                             headers=h)
    assert stats.status_code == 200, stats.text
    assert stats.json()["all_time"]["not_archived"] == 3


async def test_second_run_and_dry_run_write_nothing(client, db, headers):
    h = headers(await make_admin(db))
    ids = await make_dictionary(db)
    k1, b1 = await _structure(db)
    post = await make_post(db)
    old_ids = await _old(client, h, ids, post, 3)
    await _move(client, h, post.id, b1.id)

    dry = await fill_unit_history(db, dry_run=True)
    assert dry.dry_run and dry.rows == 6 and dry.observations == 3
    assert [(u.kind, u.observations) for u in dry.units] == [("brigade", 3), ("corps", 3)]
    assert await _kinds(db, old_ids[0]) == {}
    assert (await db.exec(select(func.count()).select_from(AuditLog).where(AuditLog.action == "fill_history"))).one() == 0

    await fill_unit_history(db)
    again = await fill_unit_history(db)
    assert again.rows == 0 and again.units == [] and again.observations == 0
    assert (await fill_unit_history(db, dry_run=True)).rows == 0


async def test_brigade_only_rows_get_the_corps(client, db, headers):
    h = headers(await make_admin(db))
    ids = await make_dictionary(db)
    k1 = await make_group(db, kind="corps")
    b1 = await make_group(db, kind="brigade")
    post = await make_post(db, group_id=b1.id)
    saved = await _old(client, h, ids, post, 2)
    assert await _kinds(db, saved[0]) == {"brigade": b1.id}
    r = await client.patch(V1.path(BrigadeRoutes.BRIGADE_BY_ID, brigade_id=b1.id), json={"corps_id": k1.id}, headers=h)
    assert r.status_code == 200, r.text

    result = await fill_unit_history(db)

    assert all([await _kinds(db, o) == {"brigade": b1.id, "corps": k1.id} for o in saved])
    assert result.rows == 2 and [(u.kind, u.observations) for u in result.units] == [("corps", 2)]


async def test_moved_post_keeps_saved_brigade(client, db, headers):
    h = headers(await make_admin(db))
    ids = await make_dictionary(db)
    k1, b1 = await _structure(db)
    k2 = await make_group(db, kind="corps")
    b2 = await make_group(db, parent_id=k2.id, kind="brigade")
    post = await make_post(db, group_id=b1.id)
    saved = await _old(client, h, ids, post, 2)
    await _move(client, h, post.id, b2.id)

    result = await fill_unit_history(db)

    assert all([await _kinds(db, o) == {"brigade": b1.id, "corps": k1.id} for o in saved])
    assert result.rows == 0 and result.units == []


async def test_units_as_create_sees_them(client, db, headers):
    h = headers(await make_admin(db))
    ids = await make_dictionary(db)
    k1, b1 = await _structure(db)
    plain = await make_group(db, parent_id=b1.id)
    b3 = await make_group(db, name="Brigade 3", kind="brigade")
    in_plain, in_archived = await make_post(db), await make_post(db)
    plain_obs = (await _old(client, h, ids, in_plain, 1))[0]
    archived_obs = (await _old(client, h, ids, in_archived, 1))[0]
    await _move(client, h, in_plain.id, plain.id)
    await _move(client, h, in_archived.id, b3.id)
    group = await db.get(ScopeGroup, b3.id)
    group.is_deleted = True
    await db.commit()

    await fill_unit_history(db)

    assert await _kinds(db, plain_obs) == {"brigade": b1.id, "corps": k1.id}
    assert await _kinds(db, archived_obs) == {"brigade": b3.id}


async def test_archived_rows_filled_and_unplaced_posts_skipped(client, db, headers):
    h = headers(await make_admin(db))
    ids = await make_dictionary(db)
    k1, b1 = await _structure(db)
    plain = await make_group(db)
    live, archived_post = await make_post(db), await make_post(db)
    at_root, in_plain = await make_post(db), await make_post(db, group_id=plain.id)
    live_obs = (await _old(client, h, ids, live, 2))
    archived_obs = live_obs[0]
    r = await client.post(V1.path(ObservationRoutes.OBSERVATION_ARCHIVE, observation_id=archived_obs), headers=h)
    assert r.status_code == 200, r.text
    post_obs = (await _old(client, h, ids, archived_post, 1))[0]
    untouched = [(await _old(client, h, ids, at_root, 1))[0], (await _old(client, h, ids, in_plain, 1))[0]]
    await _move(client, h, live.id, b1.id)
    await _move(client, h, archived_post.id, b1.id)
    r = await client.post(V1.path(PostRoutes.POST_ARCHIVE, post_id=archived_post.id), headers=h)
    assert r.status_code == 200, r.text

    result = await fill_unit_history(db)

    assert result.posts_skipped == 2
    assert result.observations == 3 and result.rows == 6
    for observation_id in (*live_obs, post_obs):
        assert await _kinds(db, observation_id) == {"corps": k1.id, "brigade": b1.id}
    assert [await _kinds(db, o) for o in untouched] == [{}, {}]


async def test_chunks_fill_every_observation_once(client, db, headers):
    h = headers(await make_admin(db))
    ids = await make_dictionary(db)
    k1, b1 = await _structure(db)
    post = await make_post(db)
    old_ids = await _old(client, h, ids, post, 5)
    await _move(client, h, post.id, b1.id)

    result = await fill_unit_history(db, chunk_size=2)

    assert result.observations == 5 and result.rows == 10
    counts = (await db.exec(select(ObservationUnit.observation_id, func.count())
                            .group_by(ObservationUnit.observation_id))).all()
    assert sorted(counts) == sorted((o, 2) for o in old_ids)
    assert (await fill_unit_history(db, chunk_size=2)).rows == 0


async def test_move_after_fill_keeps_history(client, db, headers):
    h = headers(await make_admin(db))
    ids = await make_dictionary(db)
    k1, b1 = await _structure(db)
    k2 = await make_group(db, kind="corps")
    b2 = await make_group(db, parent_id=k2.id, kind="brigade")
    post = await make_post(db)
    old_ids = await _old(client, h, ids, post, 2)
    await _move(client, h, post.id, b1.id)
    await fill_unit_history(db)

    await _move(client, h, post.id, b2.id)
    new_id = await _create(client, h, post.id, ids["signal_type_id"])

    assert await _listed(client, h, corps_id=k1.id) == sorted(old_ids)
    assert await _listed(client, h, corps_id=k2.id, placement="current") == sorted([*old_ids, new_id])
    assert await _listed(client, h, corps_id=k2.id) == [new_id]


async def test_audit_row_only_when_written(client, db, headers):
    h = headers(await make_admin(db))
    ids = await make_dictionary(db)
    k1, b1 = await _structure(db)
    post = await make_post(db)
    await _old(client, h, ids, post, 3)
    await _move(client, h, post.id, b1.id)

    await fill_unit_history(db)

    row = (await db.exec(select(AuditLog).where(AuditLog.action == "fill_history"))).one()
    assert row.resource_type == "observation_units" and row.user_id is None
    assert row.new_values == {"units": {str(b1.id): 3, str(k1.id): 3}, "observations": 3, "rows": 6}
    await fill_unit_history(db)
    await fill_unit_history(db, dry_run=True)
    assert len((await db.exec(select(AuditLog).where(AuditLog.action == "fill_history"))).all()) == 1


def test_report_lines():
    result = HistoryFill(units=[UnitFill(7, "brigade", "Brigade 10", 3), UnitFill(2, "corps", "Corps 1", 3)],
                         observations=3, rows=6, posts_skipped=1, dry_run=True)
    assert report_lines(result) == [
        "dry run: nothing written",
        "brigade 7 Brigade 10: 3",
        "corps 2 Corps 1: 3",
        "observations: 3, rows: 6, posts in no unit: 1",
    ]
    written = HistoryFill(units=[], observations=0, rows=0, posts_skipped=0, dry_run=False)
    assert report_lines(written) == ["observations: 0, rows: 0, posts in no unit: 0"]


def test_parser_rejects_unknown_command():
    with pytest.raises(SystemExit):
        main(["nope"])
