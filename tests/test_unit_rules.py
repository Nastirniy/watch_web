"""Corps and brigades: the kind of a scope group, the placement rules (ALLOWED_PARENT) on every write path."""
from datetime import datetime, timezone
from typing import Optional

import pytest
from sqlmodel import select

from app.backend.api.v1 import V1
from app.backend.enums.errors import ErrorCode
from app.backend.enums.general import ScopeGroupKind
from app.backend.enums.routes import ScopeGroupRoutes
from app.backend.errors.coded import CodedError
from app.backend.models.observation_unit import ObservationUnit
from app.backend.services.scope_groups import ALLOWED_PARENT, UNIT_KINDS, check_placement
from app.backend.services.units import delete_observation_units
from tests.helpers import make_admin, make_group

PARENT_KINDS = [None, *ScopeGroupKind]


def test_rules_map_covers_every_kind():
    assert set(ALLOWED_PARENT) == set(ScopeGroupKind)
    assert UNIT_KINDS == (ScopeGroupKind.CORPS, ScopeGroupKind.BRIGADE)
    assert ALLOWED_PARENT[ScopeGroupKind.CORPS] == {None}
    assert ALLOWED_PARENT[ScopeGroupKind.BRIGADE] == {ScopeGroupKind.CORPS}
    assert ALLOWED_PARENT[ScopeGroupKind.GROUP] == {None, ScopeGroupKind.GROUP}


@pytest.mark.parametrize("kind", list(ScopeGroupKind))
@pytest.mark.parametrize("parent_kind", PARENT_KINDS)
async def test_check_placement(db, kind: ScopeGroupKind, parent_kind: Optional[ScopeGroupKind]):
    parent_id = None
    if parent_kind is not None:
        parent_id = (await make_group(db, kind=parent_kind.value)).id
    if parent_kind in ALLOWED_PARENT[kind]:
        await check_placement(db, kind, parent_id)
        return
    with pytest.raises(CodedError) as caught:
        await check_placement(db, kind, parent_id)
    assert caught.value.code is ErrorCode.SCOPE_GROUP_PARENT_NOT_ALLOWED
    assert caught.value.params == {
        "group_kind": kind.value, "parent_kind": parent_kind.value if parent_kind else "root"}


async def test_plain_group_cannot_be_created_in_a_corps_or_brigade(client, db, headers):
    h = headers(await make_admin(db))
    corps = await make_group(db, kind="corps")
    brigade = await make_group(db, parent_id=corps.id, kind="brigade")
    for parent in (corps, brigade):
        r = await client.post(V1.path(ScopeGroupRoutes.ROOT), json={"name": "Sector", "parent_id": parent.id}, headers=h)
        assert r.status_code == 409, r.text
        body = r.json()
        assert body["code"] == "scope_group_parent_not_allowed"
        assert body["group_kind"] == "group" and body["parent_kind"] == parent.kind


async def test_plain_group_cannot_move_into_a_corps(client, db, headers):
    h = headers(await make_admin(db))
    corps = await make_group(db, kind="corps")
    plain = await make_group(db)
    r = await client.patch(V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=plain.id), json={"parent_id": corps.id}, headers=h)
    assert r.status_code == 409 and r.json()["code"] == "scope_group_parent_not_allowed"


async def test_corps_cannot_move_under_a_plain_group_through_scope_groups(client, db, headers):
    h = headers(await make_admin(db))
    corps = await make_group(db, kind="corps")
    plain = await make_group(db)
    r = await client.patch(V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=corps.id), json={"parent_id": plain.id}, headers=h)
    assert r.status_code == 409, r.text
    assert r.json()["group_kind"] == "corps" and r.json()["parent_kind"] == "group"


async def test_brigade_cannot_move_to_the_root_through_scope_groups(client, db, headers):
    h = headers(await make_admin(db))
    corps = await make_group(db, kind="corps")
    brigade = await make_group(db, parent_id=corps.id, kind="brigade")
    r = await client.patch(V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=brigade.id), json={"parent_id": None}, headers=h)
    assert r.status_code == 409 and r.json()["parent_kind"] == "root"


async def test_plain_groups_still_nest(client, db, headers):
    h = headers(await make_admin(db))
    outer = await make_group(db)
    r = await client.post(V1.path(ScopeGroupRoutes.ROOT), json={"name": "Inner", "parent_id": outer.id}, headers=h)
    assert r.status_code == 200, r.text


async def test_delete_observation_units_removes_only_the_given_observations(db):
    now = datetime.now(timezone.utc)
    db.add_all([ObservationUnit(observation_id=1, group_id=10, created_at=now),
                ObservationUnit(observation_id=1, group_id=20, created_at=now),
                ObservationUnit(observation_id=2, group_id=10, created_at=now)])
    await db.commit()
    assert await delete_observation_units(db, [1]) == 2
    await db.commit()
    rows = (await db.exec(select(ObservationUnit.observation_id))).all()
    assert rows == [2]
    assert await delete_observation_units(db, []) == 0
