"""Excel report by corps and brigade: corps_ids / brigade_ids with placement, a column per unit kind after the post,
the units in the filter rows, archived units marked."""
import io

import openpyxl

from app.backend.api.v1 import V1
from app.backend.auth.scope import Node
from app.backend.enums.general import Permissions
from app.backend.enums.routes import BrigadeRoutes, CorpsRoutes, ObservationRoutes, PostRoutes
from tests.helpers import (assign, make_admin, make_dictionary, make_group, make_post, make_role, make_user,
                           observation_payload)

REPORT = V1.path(ObservationRoutes.REPORT)
ARCHIVED = " (архівовано)"


def _sheet(response):
    assert response.status_code == 200, response.text
    rows = list(openpyxl.load_workbook(io.BytesIO(response.content)).active.iter_rows(values_only=True))
    header_at = next(i for i, row in enumerate(rows) if row[0] == "Користувач")
    header = rows[header_at]
    data = [dict(zip(header, row)) for row in rows[header_at + 1:]]
    filters = {row[0]: row[1] for row in rows[:header_at] if row[0] and str(row[0]).endswith(":")}
    return header, data, filters


async def _report(client, h, **body):
    return _sheet(await client.post(REPORT, json={"report_type": "all_fields", **body}, headers=h))


async def _scenario(client, db, h):
    """corps 1 > brigades 10, 20; post P in brigade 10: o1 created there; P moves to brigade 20; o2 created there"""
    ids = await make_dictionary(db)
    corps = await make_group(db, name="Corps 1", kind="corps")
    b10 = await make_group(db, name="Brigade 10", parent_id=corps.id, kind="brigade")
    b20 = await make_group(db, name="Brigade 20", parent_id=corps.id, kind="brigade")
    post = await make_post(db, name="Post P", group_id=b10.id)

    async def observe(comment):
        r = await client.post(V1.path(ObservationRoutes.CREATE),
                              json={**observation_payload(post.id, ids["signal_type_id"]), "comment": comment}, headers=h)
        assert r.status_code == 200, r.text

    await observe("o1")
    r = await client.put(V1.path(PostRoutes.POST_BY_ID, post_id=post.id), json={"group_id": b20.id}, headers=h)
    assert r.status_code == 200, r.text
    await observe("o2")
    return corps, b10, b20


def _units(data):
    return {row["Коментар"]: (row["Корпус"], row["Бригада"]) for row in data}


async def test_unit_columns_follow_the_placement(client, db, headers):
    h = headers(await make_admin(db))
    await _scenario(client, db, h)
    header, data, _ = await _report(client, h)
    assert list(header[:4]) == ["Користувач", "Пост", "Корпус", "Бригада"]
    assert _units(data) == {"o1": ("Corps 1", "Brigade 10"), "o2": ("Corps 1", "Brigade 20")}
    _, data, _ = await _report(client, h, placement="current")
    assert _units(data) == {"o1": ("Corps 1", "Brigade 20"), "o2": ("Corps 1", "Brigade 20")}


async def test_rows_per_placement(client, db, headers):
    h = headers(await make_admin(db))
    corps, b10, b20 = await _scenario(client, db, h)
    _, data, _ = await _report(client, h, brigade_ids=[b10.id])
    assert set(_units(data)) == {"o1"}
    _, data, _ = await _report(client, h, brigade_ids=[b20.id], placement="current")
    assert set(_units(data)) == {"o1", "o2"}
    _, data, _ = await _report(client, h, brigade_ids=[b10.id], placement="current")
    assert data == []
    _, data, _ = await _report(client, h, corps_ids=[corps.id], brigade_ids=[b10.id, b20.id])
    assert set(_units(data)) == {"o1", "o2"}
    _, data, _ = await _report(client, h, brigade_ids=[corps.id])
    assert data == []


async def test_filter_rows_name_the_units_and_the_placement(client, db, headers):
    h = headers(await make_admin(db))
    corps, b10, _ = await _scenario(client, db, h)
    _, _, filters = await _report(client, h, corps_ids=[corps.id], brigade_ids=[b10.id], placement="current")
    assert filters["Корпуси:"] == f"Corps 1 (#{corps.id})"
    assert filters["Бригади:"] == f"Brigade 10 (#{b10.id})"
    assert filters["Належність:"] == "поточна (де пост зараз)"
    _, _, filters = await _report(client, h, brigade_ids=[b10.id])
    assert filters["Належність:"] == "на момент створення спостереження"
    _, _, filters = await _report(client, h)
    assert "Належність:" not in filters


async def test_archived_units_are_marked(client, db, headers):
    h = headers(await make_admin(db))
    corps, b10, _ = await _scenario(client, db, h)
    r = await client.post(V1.path(BrigadeRoutes.BRIGADE_ARCHIVE, brigade_id=b10.id), headers=h)
    assert r.status_code == 200, r.text
    _, data, filters = await _report(client, h, brigade_ids=[b10.id])
    assert _units(data) == {"o1": ("Corps 1", "Brigade 10" + ARCHIVED)}
    assert filters["Бригади:"] == f"Brigade 10{ARCHIVED} (#{b10.id})"


async def test_worked_example(client, db, headers):
    """The owner's scenario: brigade 10 with posts A, B, C (4, 3, 2 observations); brigade 10 archived with cascade,
    then corps 1; all restored one by one; post B moves to brigade 20. Report by brigade 10."""
    admin = await make_admin(db)
    h = headers(admin)
    ids = await make_dictionary(db)
    corps = await make_group(db, name="Corps 1", kind="corps")
    b10 = await make_group(db, name="Brigade 10", parent_id=corps.id, kind="brigade")
    b20 = await make_group(db, name="Brigade 20", parent_id=corps.id, kind="brigade")
    posts = {name: await make_post(db, name=f"Post {name}", group_id=b10.id) for name in "ABC"}
    for name, count in (("A", 4), ("B", 3), ("C", 2)):
        for i in range(count):
            r = await client.post(V1.path(ObservationRoutes.CREATE), json={
                **observation_payload(posts[name].id, ids["signal_type_id"]), "comment": f"{name}{i}"}, headers=h)
            assert r.status_code == 200, r.text

    for path, params in ((V1.path(BrigadeRoutes.BRIGADE_ARCHIVE, brigade_id=b10.id), {"cascade": "true"}),
                         (V1.path(CorpsRoutes.CORPS_ARCHIVE, corps_id=corps.id), {"cascade": "true"}),
                         (V1.path(CorpsRoutes.CORPS_RESTORE, corps_id=corps.id), {}),
                         (V1.path(BrigadeRoutes.BRIGADE_RESTORE, brigade_id=b10.id), {}),
                         (V1.path(BrigadeRoutes.BRIGADE_RESTORE, brigade_id=b20.id), {}),
                         *((V1.path(PostRoutes.POST_RESTORE, post_id=post.id), {}) for post in posts.values())):
        r = await client.post(path, params=params, headers=h)
        assert r.status_code == 200, (path, r.text)
    r = await client.put(V1.path(PostRoutes.POST_BY_ID, post_id=posts["B"].id), json={"group_id": b20.id}, headers=h)
    assert r.status_code == 200, r.text

    role = await make_role(db, [Permissions.OBSERVATION_VIEW, Permissions.EXPORT_CREATE])
    at_corps = await make_user(db)
    await assign(db, at_corps, role, Node.group(corps.id))
    at_brigade = await make_user(db)
    await assign(db, at_brigade, role, Node.group(b10.id))

    async def rows(user, placement):
        _, data, _ = await _report(client, headers(user), brigade_ids=[b10.id], placement=placement)
        return data

    recorded = await rows(at_corps, "recorded")
    assert len(recorded) == 9
    assert {row["Бригада"] for row in recorded if row["Коментар"].startswith("B")} == {"Brigade 10"}
    assert len(await rows(at_corps, "current")) == 6
    assert len(await rows(at_brigade, "recorded")) == 6
    assert len(await rows(at_brigade, "current")) == 6
