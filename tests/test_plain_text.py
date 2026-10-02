"""User-entered text is stored and returned as plain characters, never as HTML entities.

The old input filter ran html.escape, so rows like "кур&#x27;єр" exist in
production databases. Responses must decode them, and new input must not be
escaped in the first place.
"""
import pytest
from sqlmodel import select

from app.backend.enums.routes import PostRoutes, TagRoutes, ObservationRoutes, SearchRoutes
from app.backend.api.v1 import V1
from app.backend.models.observation import Tag
from app.backend.models.post import Post
from app.backend.utils.text import unescape_html, unescape_deep
from app.backend.utils.validators import SecurityValidators, PlainTextModel
from tests.helpers import make_admin, make_post, make_dictionary, make_observation

POST = V1.path(PostRoutes.POST_BY_ID)
POSTS = V1.path(PostRoutes.ROOT)
TAG = V1.path(TagRoutes.TAG_BY_ID)
OBS_LIST = V1.path(ObservationRoutes.LIST_ALL)


@pytest.mark.parametrize("escaped, plain", [
    ("кур&#x27;єр", "кур'єр"),
    ("кур&#39;єр", "кур'єр"),
    ("кур&x27;єр", "кур'єр"),
    ("кур&amp;#x27;єр", "кур'єр"),
    ("&quot;Північ&quot;", '"Північ"'),
    ("AT&amp;T", "AT&T"),
    ("a &lt;b&gt; c", "a <b> c"),
    ("AT&T", "AT&T"),
    ("plain text", "plain text"),
    ("", ""),
])
def test_unescape_html(escaped, plain):
    assert unescape_html(escaped) == plain


def test_unescape_deep_walks_containers_and_leaves_other_types():
    value = {"name": "кур&#x27;єр", "tags": ["a&amp;b", 3], "nested": {"x": ("&#39;", None)}, "n": 1.5}
    assert unescape_deep(value) == {"name": "кур'єр", "tags": ["a&b", 3], "nested": {"x": ("'", None)}, "n": 1.5}


def test_sanitize_string_keeps_plain_characters_and_decodes_entities():
    assert SecurityValidators.sanitize_string("кур'єр") == "кур'єр"
    assert SecurityValidators.sanitize_string('Пост "Північ" & Co') == 'Пост "Північ" & Co'
    assert SecurityValidators.sanitize_string("кур&#x27;єр") == "кур'єр"
    with pytest.raises(ValueError):
        SecurityValidators.sanitize_string("&lt;script&gt;alert(1)&lt;/script&gt;")


def test_plain_text_model_decodes_strings_in_every_field():
    class Item(PlainTextModel):
        name: str
        notes: list
        extra: dict
        count: int

    item = Item(name="кур&#x27;єр", notes=["a&amp;b"], extra={"k": "&quot;q&quot;"}, count=2)
    assert item.name == "кур'єр"
    assert item.notes == ["a&b"]
    assert item.extra == {"k": '"q"'}
    assert item.count == 2


@pytest.mark.asyncio
async def test_escaped_rows_are_returned_as_plain_text(client, db, headers):
    admin = await make_admin(db)
    post = await make_post(db, name="Пост &#x27;Північ&#x27;")
    post.description = "AT&amp;T"
    admin.username_alias = "Мар&#x27;яна"
    tag = Tag(name="кур&#x27;єр")
    db.add(tag)
    await db.commit()
    await db.refresh(tag)

    response = await client.get(POST.format(post_id=post.id), headers=headers(admin))
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Пост 'Північ'"
    assert response.json()["description"] == "AT&T"

    response = await client.get(TAG.format(tag_id=tag.id), headers=headers(admin))
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "кур'єр"

    dictionary = await make_dictionary(db)
    observation = await make_observation(db, admin, post, dictionary["signal_type_id"])
    observation.comment = "зв&#x27;язок &quot;A&quot;"
    await db.commit()

    response = await client.get(OBS_LIST, headers=headers(admin))
    assert response.status_code == 200, response.text
    body = next(item for item in response.json() if item["id"] == observation.id)
    assert body["comment"] == "зв'язок \"A\""
    assert body["user"]["username_alias"] == "Мар'яна"

    response = await client.get(V1.path(SearchRoutes.POSTS), headers=headers(admin))
    assert response.status_code == 200, response.text
    names = {item["name"] for item in response.json()["items"]}
    assert "Пост 'Північ'" in names


@pytest.mark.asyncio
async def test_new_input_is_stored_without_entities(client, db, headers):
    admin = await make_admin(db)
    payload = {"name": "Пост 'Північ'", "code": "PN-1", "description": 'Зв\'язок "A" & B'}

    response = await client.post(POSTS, json=payload, headers=headers(admin))
    assert response.status_code in (200, 201), response.text
    assert response.json()["name"] == payload["name"]
    assert response.json()["description"] == payload["description"]

    stored = (await db.exec(select(Post).where(Post.code == "PN-1"))).one()
    assert stored.name == payload["name"]
    assert stored.description == payload["description"]
    assert "&#" not in stored.name and "&amp;" not in stored.description
