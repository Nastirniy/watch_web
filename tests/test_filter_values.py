"""Filter values the API checks (found by the live filter test, 2026-10-19): an unknown query parameter, a status,
confidence level or media type outside its set, and last_n_days next to an absolute observed_at bound answer 422
instead of an unfiltered or empty page; a file extension matches with or without its dot."""
import io

import openpyxl
import pytest

from app.backend.api.v1 import V1
from app.backend.enums.routes import (
    CategoryRoutes, CertificateRoutes, MediaFormatRoutes, MediaRoutes, ObservationRoutes, PostRoutes, RoleRoutes,
    ScopeGroupRoutes, SearchRoutes, SignalTypeRoutes, TagRoutes, UserRoutes,
)
from app.backend.models.observation import SignalType
from tests.helpers import make_admin, make_dictionary, make_media, make_observation, make_post


def _problem_items(response):
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == "validation_error"
    return body["errors"]


def _item_for(response, parameter):
    items = [item for item in _problem_items(response) if item.get("parameter") == parameter]
    assert items, response.json()["errors"]
    return items[0]


FILTER_URLS = [
    V1.path(route) for route in (
        ObservationRoutes.LIST_ALL, MediaRoutes.ROOT, PostRoutes.ROOT, UserRoutes.ROOT, RoleRoutes.ROOT,
        TagRoutes.LIST_ALL, CategoryRoutes.LIST_ALL, SignalTypeRoutes.LIST_ALL, MediaFormatRoutes.LIST_ALL,
        ScopeGroupRoutes.ROOT,
        CertificateRoutes.ROOT, SearchRoutes.OBSERVATIONS, SearchRoutes.MEDIA, SearchRoutes.POSTS, SearchRoutes.USERS,
        SearchRoutes.ROLES, SearchRoutes.TAGS, SearchRoutes.CATEGORIES, SearchRoutes.SIGNAL_TYPES,
        SearchRoutes.MEDIA_FORMATS,
    )
]


@pytest.mark.asyncio
@pytest.mark.parametrize("url", FILTER_URLS)
async def test_an_unknown_filter_parameter_is_422_not_an_unfiltered_list(client, db, headers, url):
    admin = await make_admin(db)
    response = await client.get(url, params={"comment_contain": "x"}, headers=headers(admin))
    assert _item_for(response, "comment_contain")["code"] == "extra_forbidden"


@pytest.mark.asyncio
@pytest.mark.parametrize("search", [False, True])
@pytest.mark.parametrize("params, parameter", [
    ({"status": "bogus"}, "status"),
    ({"status_in": ["active", "bogus"]}, "status_in.1"),
    ({"confidence_level": "certain"}, "confidence_level"),
])
async def test_observation_values_outside_their_set_are_422(client, db, headers, search, params, parameter):
    admin = await make_admin(db)
    url = V1.path(SearchRoutes.OBSERVATIONS if search else ObservationRoutes.LIST_ALL)
    response = await client.get(url, params=params, headers=headers(admin))
    assert _item_for(response, parameter)["code"] == "enum"


@pytest.mark.asyncio
@pytest.mark.parametrize("url, parameter", [
    (V1.path(MediaRoutes.ROOT), "media_type"),
    (V1.path(SearchRoutes.MEDIA), "media_type"),
    (V1.path(MediaFormatRoutes.LIST_ALL), "media_type"),
    (V1.path(SearchRoutes.MEDIA_FORMATS), "media_type"),
])
async def test_a_media_type_outside_its_set_is_422(client, db, headers, url, parameter):
    admin = await make_admin(db)
    response = await client.get(url, params={parameter: "picture"}, headers=headers(admin))
    assert _item_for(response, parameter)["code"] == "enum"


@pytest.mark.asyncio
@pytest.mark.parametrize("search", [False, True])
async def test_known_statuses_and_levels_still_filter(client, db, headers, search):
    admin = await make_admin(db)
    post = await make_post(db)
    ids = await make_dictionary(db)
    observation = await make_observation(db, admin, post, ids["signal_type_id"])
    url = V1.path(SearchRoutes.OBSERVATIONS if search else ObservationRoutes.LIST_ALL)
    for params, found in (
        ({"status": "active"}, True), ({"status": "confirmed"}, False),
        ({"status_in": ["confirmed", "active"]}, True), ({"confidence_level": "medium"}, True),
        ({"confidence_level": "high"}, False),
    ):
        response = await client.get(url, params=params, headers=headers(admin))
        assert response.status_code == 200, response.text
        items = response.json()["items"] if search else response.json()
        assert (observation.id in {item["id"] for item in items}) is found, params


@pytest.mark.asyncio
@pytest.mark.parametrize("search", [False, True])
@pytest.mark.parametrize("bound", ["observed_at_from", "observed_at_to"])
async def test_last_n_days_with_an_observed_at_bound_is_422(client, db, headers, search, bound):
    admin = await make_admin(db)
    url = V1.path(SearchRoutes.OBSERVATIONS if search else ObservationRoutes.LIST_ALL)
    response = await client.get(url, params={"last_n_days": 7, bound: "2026-10-01T00:00:00Z"}, headers=headers(admin))
    assert _item_for(response, "last_n_days")["code"] == "date_filters_conflict"


@pytest.mark.asyncio
@pytest.mark.parametrize("search", [False, True])
@pytest.mark.parametrize("spelling", ["png", ".png", "PNG", ".PNG"])
async def test_file_extension_matches_with_or_without_the_dot(client, db, headers, search, spelling):
    admin = await make_admin(db)
    post = await make_post(db)
    ids = await make_dictionary(db)
    observation = await make_observation(db, admin, post, ids["signal_type_id"])
    png = await make_media(db, observation, admin, file_extension=".png", mime_type="image/png")
    await make_media(db, observation, admin)
    url = V1.path(SearchRoutes.MEDIA if search else MediaRoutes.ROOT)
    response = await client.get(url, params={"file_extension": spelling}, headers=headers(admin))
    assert response.status_code == 200, response.text
    items = response.json()["items"] if search else response.json()
    assert [item["id"] for item in items] == [png.id]


def _report_rows(response, signal_name):
    assert response.status_code == 200, response.text
    book = openpyxl.load_workbook(io.BytesIO(response.content), read_only=True)
    return [row for sheet in book.worksheets for row in sheet.iter_rows(values_only=True) if signal_name in row]


@pytest.mark.asyncio
@pytest.mark.parametrize("body, pointer, code", [
    ({"statuses": ["active", "bogus"]}, "#/statuses/1", "enum"),
    ({"confidence_level": "certain"}, "#/confidence_level", "enum"),
    ({"status": "active"}, "#/status", "extra_forbidden"),
])
async def test_report_body_values_outside_their_set_are_422(client, db, headers, body, pointer, code):
    admin = await make_admin(db)
    response = await client.post(V1.path(ObservationRoutes.REPORT), json={"report_type": "all_fields", **body},
                                 headers=headers(admin))
    items = [item for item in _problem_items(response) if item.get("pointer") == pointer]
    assert items and items[0]["code"] == code, response.json()["errors"]


@pytest.mark.asyncio
async def test_report_statuses_and_level_still_filter(client, db, headers):
    admin = await make_admin(db)
    post = await make_post(db)
    ids = await make_dictionary(db)
    await make_observation(db, admin, post, ids["signal_type_id"])
    signal_name = (await db.get(SignalType, ids["signal_type_id"])).name
    url = V1.path(ObservationRoutes.REPORT)
    for body, rows in (
        ({"statuses": ["active"]}, 1), ({"statuses": ["confirmed"]}, 0),
        ({"confidence_level": "medium"}, 1), ({"confidence_level": "low"}, 0),
    ):
        response = await client.post(url, json={"report_type": "all_fields", **body}, headers=headers(admin))
        assert len(_report_rows(response, signal_name)) == rows, body


@pytest.mark.asyncio
@pytest.mark.parametrize("search", [False, True])
@pytest.mark.parametrize("spelling", ["video/matroska", "video/x-matroska", "Video/X-Matroska", "video/matroska; codecs=vp9"])
async def test_mime_type_filter_accepts_an_alias(client, db, headers, search, spelling):
    """Stored types are canonical (032): the filter parses its value and maps an alias to the canonical name"""
    admin = await make_admin(db)
    post = await make_post(db)
    ids = await make_dictionary(db)
    observation = await make_observation(db, admin, post, ids["signal_type_id"])
    mkv = await make_media(db, observation, admin, media_type="video", file_extension=".mkv", mime_type="video/matroska")
    await make_media(db, observation, admin, media_type="video")
    url = V1.path(SearchRoutes.MEDIA if search else MediaRoutes.ROOT)
    response = await client.get(url, params={"mime_type": spelling}, headers=headers(admin))
    assert response.status_code == 200, response.text
    items = response.json()["items"] if search else response.json()
    assert [item["id"] for item in items] == [mkv.id]


@pytest.mark.asyncio
@pytest.mark.parametrize("search", [False, True])
async def test_mime_type_filter_finds_a_row_under_its_own_spelling(client, db, headers, search):
    """A row 032 left under an alias (an x-matroska file named .webm) stays findable by that spelling: the filter
    matches the parsed value and its canonical name"""
    admin = await make_admin(db)
    post = await make_post(db)
    ids = await make_dictionary(db)
    observation = await make_observation(db, admin, post, ids["signal_type_id"])
    canonical = await make_media(db, observation, admin, media_type="video", file_extension=".mkv",
                                 mime_type="video/matroska")
    alias = await make_media(db, observation, admin, media_type="video", file_extension=".webm",
                             mime_type="video/x-matroska")
    await make_media(db, observation, admin, media_type="video")
    url = V1.path(SearchRoutes.MEDIA if search else MediaRoutes.ROOT)
    for spelling, expected in (("Video/X-Matroska", {canonical.id, alias.id}), ("video/matroska", {canonical.id})):
        response = await client.get(url, params={"mime_type": spelling}, headers=headers(admin))
        assert response.status_code == 200, response.text
        items = response.json()["items"] if search else response.json()
        assert {item["id"] for item in items} == expected, spelling
