"""Findings of the stage 2 review: report filter rows name only units the caller may see, one subquery per unit
kind, recorded unit stats read the created_at range of observation_units."""
import io
import re

import openpyxl
import pytest
from sqlalchemy import event
from sqlalchemy.dialects import sqlite

from app.backend.api.v1 import V1
from app.backend.auth.scope import Node, ScopeTree
from app.backend.enums.general import Permissions, ScopeGroupKind, UnitPlacement
from app.backend.enums.routes import BrigadeRoutes, ObservationRoutes
from app.backend.services.units import observation_units_any
from tests.helpers import assign, make_admin, make_dictionary, make_group, make_post, make_role, make_user, observation_payload


@pytest.fixture
def statements(engine):
    seen = []

    def record(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    yield seen
    event.remove(engine.sync_engine, "before_cursor_execute", record)


async def test_report_names_only_units_the_caller_sees(client, db, headers):
    mine = await make_group(db, name="Corps Mine", kind="corps")
    secret = await make_group(db, name="Corps Secret", kind="corps")
    plain = await make_group(db, name="Plain Secret")
    user = await make_user(db)
    await assign(db, user, await make_role(db, [Permissions.OBSERVATION_VIEW, Permissions.EXPORT_CREATE]),
                 Node.group(mine.id))
    r = await client.post(V1.path(ObservationRoutes.REPORT), json={
        "report_type": "all_fields", "corps_ids": [mine.id, secret.id, plain.id]}, headers=headers(user))
    assert r.status_code == 200, r.text
    cells = [str(v) for row in openpyxl.load_workbook(io.BytesIO(r.content)).active.iter_rows(values_only=True)
             for v in row if v is not None]
    corps_row = next(cells[i + 1] for i, v in enumerate(cells) if v == "Корпуси:")
    assert corps_row == f"Corps Mine (#{mine.id}), #{secret.id}, #{plain.id}"
    assert not any("Secret" in v for v in cells)


def test_several_units_of_a_kind_are_one_subquery():
    tree = ScopeTree(group_parent={1: None, 2: None, 3: 1}, archived_groups=frozenset(), post_group={7: 3, 8: 2},
                     archived_posts=frozenset(), group_kind={1: "corps", 2: "corps", 3: "brigade"})
    recorded = str(observation_units_any(tree, {ScopeGroupKind.CORPS: [1, 2], ScopeGroupKind.BRIGADE: None},
                                         UnitPlacement.RECORDED).compile(dialect=sqlite.dialect()))
    assert recorded.count("FROM observation_units") == 1 and " OR " not in recorded, recorded
    current = str(observation_units_any(tree, {ScopeGroupKind.CORPS: [1, 2]}, UnitPlacement.CURRENT)
                  .compile(dialect=sqlite.dialect(), compile_kwargs={"literal_binds": True}))
    assert " OR " not in current and re.search(r"post_id IN \(7, 8\)", current), current


async def test_recorded_unit_stats_read_the_period_range_of_observation_units(client, db, headers, statements):
    h = headers(await make_admin(db))
    ids = await make_dictionary(db)
    corps = await make_group(db, kind="corps")
    brigade = await make_group(db, parent_id=corps.id, kind="brigade")
    post = await make_post(db, group_id=brigade.id)
    r = await client.post(V1.path(ObservationRoutes.CREATE), json=observation_payload(post.id, ids["signal_type_id"]),
                          headers=h)
    assert r.status_code == 200, r.text
    statements.clear()
    r = await client.get(V1.path(BrigadeRoutes.BRIGADE_OBSERVATION_STATS, brigade_id=brigade.id), params={"tz": "UTC"},
                         headers=h)
    assert r.status_code == 200 and r.json()["period"]["not_archived"] == 1, r.text
    ranged = [s for s in statements if "FROM observation_units" in s and "observation_units.created_at >=" in s]
    assert ranged, statements
