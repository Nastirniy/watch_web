"""Archived tags stay attached to observations, are visible, and keep filtering and exports correct."""
import pytest
from sqlmodel import select

from app.backend.enums.routes import ObservationRoutes, SearchRoutes, TagRoutes
from app.backend.api.v1 import V1
from app.backend.models.observation import Observation, Tag
from app.backend.services.observation_report_service import _tag_label, _tag_filter_label, _build_filter_rows
from app.backend.enums.general import ObservationReportType, TagMatch
from app.backend.filters.observation_report_filter import ObservationReportCriteria, ObservationReportFilter
from tests.helpers import make_post, make_user, make_admin, make_dictionary, make_observation

LIST = V1.path(ObservationRoutes.LIST_ALL)
UPDATE = V1.path(ObservationRoutes.OBSERVATION_BY_ID)
SEARCH = V1.path(SearchRoutes.OBSERVATIONS)


async def attach_tag(db, observation, tag_id):
    tag = (await db.exec(select(Tag).where(Tag.id == tag_id))).one()
    loaded = (await db.exec(select(Observation).where(Observation.id == observation.id))).one()
    await db.refresh(loaded, ["tags"])
    loaded.tags.append(tag)
    db.add(loaded)
    await db.commit()


@pytest.mark.asyncio
async def test_deleted_tag_stays_visible_and_keepable(client, db, headers):
    post = await make_post(db)
    ids = await make_dictionary(db)
    other = await make_dictionary(db)
    admin = await make_admin(db)
    user = await make_user(db, perms=["observation.view", "observation.update"], post_id=post.id)
    observation = await make_observation(db, user, post, ids["signal_type_id"])
    await attach_tag(db, observation, ids["tag_id"])

    listed = (await client.get(LIST, headers=headers(user))).json()
    assert listed[0]["tags"] == [{"id": ids["tag_id"], "name": listed[0]["tags"][0]["name"], "is_deleted": False}]

    deleted = await client.post(V1.path(TagRoutes.TAG_ARCHIVE, tag_id=ids["tag_id"]), headers=headers(admin))
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["is_deleted"] is True
    deleted_other = await client.post(V1.path(TagRoutes.TAG_ARCHIVE, tag_id=other["tag_id"]), headers=headers(admin))
    assert deleted_other.status_code == 200

    listed = (await client.get(LIST, headers=headers(user))).json()
    assert listed[0]["tag_ids"] == [ids["tag_id"]]
    assert listed[0]["tags"][0]["is_deleted"] is True

    # filtering by the deleted tag id still finds the observation
    filtered = (await client.get(LIST, params={"tag_ids": str(ids["tag_id"])}, headers=headers(user))).json()
    assert [o["id"] for o in filtered] == [observation.id]

    # keeping the attached deleted tag on update is allowed
    kept = await client.put(UPDATE.format(observation_id=observation.id),
                            json={"comment": "edited", "tag_ids": [ids["tag_id"]]}, headers=headers(user))
    assert kept.status_code == 200, kept.text
    assert kept.json()["tag_ids"] == [ids["tag_id"]]

    # adding another deleted tag that was not attached is rejected
    rejected = await client.put(UPDATE.format(observation_id=observation.id),
                                json={"tag_ids": [ids["tag_id"], other["tag_id"]]}, headers=headers(user))
    assert rejected.status_code in (400, 404), rejected.text

    # dropping it explicitly still works
    dropped = await client.put(UPDATE.format(observation_id=observation.id), json={"tag_ids": []}, headers=headers(user))
    assert dropped.status_code == 200 and dropped.json()["tags"] == []


def test_report_labels_mark_deleted_tags():
    class T:
        def __init__(self, name, is_deleted):
            self.name, self.is_deleted = name, is_deleted

    assert _tag_label(T("alpha", False)) == "alpha"
    assert _tag_label(T("alpha", True)).startswith("alpha (")
    assert _tag_filter_label(7, {7: "alpha"}) == "alpha (#7)"
    assert _tag_filter_label(8, {7: "alpha"}) == "#8"
    rows = _build_filter_rows(ObservationReportCriteria(report_type=ObservationReportType.ALL_FIELDS, tag_ids=[7, 8]), {7: "alpha"})
    tag_row = [r for r in rows if "alpha (#7)" in r[1]]
    assert tag_row and "#8" in tag_row[0][1]


async def tagged_scene(db):
    """Observations tagged {a, b}, {a}, {b} and none, all visible to one user"""
    post = await make_post(db)
    a, b = await make_dictionary(db), await make_dictionary(db)
    user = await make_user(db, perms=["observation.view"], post_id=post.id)
    both, only_a, only_b, untagged = [await make_observation(db, user, post, a["signal_type_id"]) for _ in range(4)]
    for observation, tags in ((both, [a, b]), (only_a, [a]), (only_b, [b])):
        for tag in tags:
            await attach_tag(db, observation, tag["tag_id"])
    return user, a["tag_id"], b["tag_id"], {"both": both.id, "only_a": only_a.id, "only_b": only_b.id}


@pytest.mark.asyncio
async def test_tag_filter_matches_any_or_all_of_the_tags(client, db, headers):
    user, a, b, ids = await tagged_scene(db)

    async def listed(**params):
        response = await client.get(LIST, params=params, headers=headers(user))
        assert response.status_code == 200, response.text
        return {observation["id"] for observation in response.json()}

    assert await listed(tag_ids=[a, b]) == {ids["both"], ids["only_a"], ids["only_b"]}, "any of them by default"
    assert await listed(tag_ids=[a, b], tag_match="any") == {ids["both"], ids["only_a"], ids["only_b"]}
    assert await listed(tag_ids=[a, b], tag_match="all") == {ids["both"]}
    assert await listed(tag_ids=[a, a, b], tag_match="all") == {ids["both"]}, "a repeated id is one tag"
    assert await listed(tag_ids=[a], tag_match="all") == {ids["both"], ids["only_a"]}


@pytest.mark.asyncio
async def test_search_total_follows_the_tag_match(client, db, headers):
    user, a, b, ids = await tagged_scene(db)
    page = (await client.get(SEARCH, params={"tag_ids": [a, b], "tag_match": "all"}, headers=headers(user))).json()
    assert [item["id"] for item in page["items"]] == [ids["both"]] and page["total"] == 1
    page = (await client.get(SEARCH, params={"tag_ids": [a, b]}, headers=headers(user))).json()
    assert page["total"] == 3


@pytest.mark.asyncio
async def test_report_filter_matches_any_or_all_of_the_tags(db):
    _, a, b, ids = await tagged_scene(db)

    async def reported(**criteria):
        query = ObservationReportFilter.build_query(
            ObservationReportCriteria(report_type=ObservationReportType.ALL_FIELDS, tag_ids=[a, b], **criteria),
            unit_condition=None)
        return {band.observation_id for band in (await db.exec(query)).all()}

    assert await reported() == {ids["both"], ids["only_a"], ids["only_b"]}
    assert await reported(tag_match=TagMatch.ALL) == {ids["both"]}


def test_report_header_names_the_tag_match():
    def tag_row(**criteria):
        rows = _build_filter_rows(ObservationReportCriteria(report_type=ObservationReportType.ALL_FIELDS, tag_ids=[7], **criteria))
        return [label for label, _ in rows if label.startswith("Теги")]

    assert tag_row() == ["Теги (будь-який):"]
    assert tag_row(tag_match=TagMatch.ALL) == ["Теги (усі):"]
