"""Findings of the stage 1 review: the /scope-groups writers take the unit lock order, brigade placement answers only
declared codes, no lock on a group id that may not exist."""
import re

import pytest
from sqlalchemy import event

from app.backend.api.shared.helpers.scope_group_helper import lock_groups
from app.backend.api.v1 import V1
from app.backend.auth.scope import ScopeTree
from app.backend.enums.routes import BrigadeRoutes, ScopeGroupRoutes
from app.backend.services.scope_groups import MAX_GROUP_DEPTH
from tests.helpers import make_admin, make_group


@pytest.fixture
def group_reads(engine):
    """The ids of the scope_groups rows read by primary key, in order"""
    seen = []

    def record(conn, cursor, statement, parameters, context, executemany):
        if re.match(r"\s*SELECT .*\sFROM scope_groups\s+WHERE scope_groups\.id = \?", statement, re.S | re.I):
            seen.append(parameters[0])

    event.listen(engine.sync_engine, "before_cursor_execute", record)
    yield seen
    event.remove(engine.sync_engine, "before_cursor_execute", record)


async def test_scope_group_move_locks_both_parents_before_the_group(client, db, headers, group_reads):
    h = headers(await make_admin(db))
    c1 = await make_group(db, kind="corps")
    c2 = await make_group(db, kind="corps")
    brigade = await make_group(db, parent_id=c1.id, kind="brigade")
    group_reads.clear()
    r = await client.patch(V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=brigade.id),
                           json={"parent_id": c2.id, "expected_parent_id": c1.id}, headers=h)
    assert r.status_code == 200, r.text
    assert group_reads[:3] == [c1.id, c2.id, brigade.id], group_reads


async def test_scope_group_restore_locks_the_parent_before_the_group(client, db, headers, group_reads):
    h = headers(await make_admin(db))
    corps = await make_group(db, kind="corps")
    brigade = await make_group(db, parent_id=corps.id, kind="brigade")
    brigade.is_deleted = True
    db.add(brigade)
    await db.commit()
    group_reads.clear()
    r = await client.post(V1.path(ScopeGroupRoutes.GROUP_RESTORE, group_id=brigade.id), headers=h)
    assert r.status_code == 200, r.text
    assert group_reads[:2] == [corps.id, brigade.id], group_reads


async def test_brigade_into_itself_or_a_deep_plain_group_is_parent_not_allowed(client, db, headers):
    h = headers(await make_admin(db))
    corps = await make_group(db, kind="corps")
    brigade = await make_group(db, parent_id=corps.id, kind="brigade")
    r = await client.patch(V1.path(BrigadeRoutes.BRIGADE_BY_ID, brigade_id=brigade.id), json={"corps_id": brigade.id},
                           headers=h)
    assert r.status_code == 409 and r.json()["code"] == "scope_group_parent_not_allowed", r.text
    parent = None
    for _ in range(MAX_GROUP_DEPTH):
        parent = (await make_group(db, parent_id=parent)).id
    for r in (await client.post(V1.path(BrigadeRoutes.ROOT), json={"corps_id": parent, "name": "B"}, headers=h),
              await client.patch(V1.path(BrigadeRoutes.BRIGADE_BY_ID, brigade_id=brigade.id), json={"corps_id": parent},
                                 headers=h)):
        assert r.status_code == 409 and r.json()["code"] == "scope_group_parent_not_allowed", r.text


async def test_lock_groups_never_locks_a_group_the_tree_does_not_know(db, group_reads):
    tree = ScopeTree(group_parent={1: None}, archived_groups=frozenset(), post_group={}, archived_posts=frozenset())
    assert await lock_groups(db, tree, [99999, None]) is False
    assert group_reads == []
