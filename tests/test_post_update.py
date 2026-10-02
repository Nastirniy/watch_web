"""POST /posts/ and PUT /posts/{id}: required fields, code uniqueness and updated_at."""
import pytest
from sqlmodel import select

from app.backend.enums.routes import PostRoutes
from app.backend.models.post import Post
from tests.helpers import make_admin, make_post

CREATE = PostRoutes.ROOT.versioned_path()
POST = PostRoutes.POST_BY_ID.versioned_path()


def field_error(response, field: str) -> dict:
    assert response.status_code == 422, response.text
    errors = [e for e in response.json()["errors"] if e["pointer"] == f"#/{field}"]
    assert len(errors) == 1, response.json()
    return errors[0]


@pytest.mark.asyncio
async def test_update_code(client, db, headers):
    admin = await make_admin(db)
    post = await make_post(db)
    before = post.updated_at
    response = await client.put(POST.format(post_id=post.id), headers=headers(admin), json={"code": "NEW-01"})
    assert response.status_code == 200, response.text
    assert response.json()["code"] == "NEW-01"

    await db.refresh(post)
    assert post.updated_at > before


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["name", "code", "is_active"])
async def test_update_refuses_null_for_not_null_fields(client, db, headers, field):
    """null used to reach the NOT NULL column and answer 500"""
    admin = await make_admin(db)
    post = await make_post(db)
    response = await client.put(POST.format(post_id=post.id), headers=headers(admin), json={field: None})
    assert field_error(response, field)["code"] == "value_null"


@pytest.mark.asyncio
async def test_update_accepts_null_for_optional_fields(client, db, headers):
    admin = await make_admin(db)
    post = await make_post(db)
    response = await client.put(POST.format(post_id=post.id), headers=headers(admin), json={"city": None, "region": None})
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["name", "code"])
async def test_spaces_only_are_refused(client, db, headers, field):
    """"   " passed min_length before the strip and stored an empty value"""
    admin = await make_admin(db)
    post = await make_post(db)
    response = await client.put(POST.format(post_id=post.id), headers=headers(admin), json={field: "   "})
    assert field_error(response, field)["code"] == "value_empty"

    payload = {"name": "Name", "code": "CODE-1", field: "   "}
    response = await client.post(CREATE, headers=headers(admin), json=payload)
    assert field_error(response, field)["code"] == "value_empty"


@pytest.mark.asyncio
async def test_code_uniqueness_ignores_case(client, db, headers):
    admin = await make_admin(db)
    taken = await make_post(db)
    other = await make_post(db)

    response = await client.put(POST.format(post_id=other.id), headers=headers(admin), json={"code": taken.code.lower()})
    assert response.status_code == 400
    assert response.json()["code"] == "post_code_taken"

    response = await client.post(CREATE, headers=headers(admin), json={"name": "Name", "code": taken.code.lower()})
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_post_can_change_only_the_case_of_own_code(client, db, headers):
    admin = await make_admin(db)
    post = await make_post(db)
    response = await client.put(POST.format(post_id=post.id), headers=headers(admin), json={"code": post.code.lower()})
    assert response.status_code == 200, response.text
    assert response.json()["code"] == post.code.lower()


@pytest.mark.asyncio
async def test_code_race_answers_400(client, db, headers, monkeypatch):
    """Another request took the code between the check and the commit: the unique index answers, reported as 400"""
    from app.backend.routes import posts as posts_routes
    monkeypatch.setattr(posts_routes, "_code_taken", lambda *args, **kwargs: select(Post).where(Post.id == -1))

    admin = await make_admin(db)
    taken = await make_post(db)
    other = await make_post(db)
    response = await client.put(POST.format(post_id=other.id), headers=headers(admin), json={"code": taken.code})
    assert response.status_code == 400, response.text
    assert response.json()["code"] == "post_code_taken"

    response = await client.post(CREATE, headers=headers(admin), json={"name": "Name", "code": taken.code})
    assert response.status_code == 400, response.text
