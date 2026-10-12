"""Contract of /auth/me, /auth/check-permission and token handling."""
import pytest

from app.backend.enums.routes import AuthRoutes
from app.backend.api.v1 import V1
from app.backend.auth.service import AuthService
from tests.helpers import make_post, make_user, make_admin

ME = V1.path(AuthRoutes.ME)
CHECK = V1.path(AuthRoutes.CHECK_PERMISSION)


@pytest.mark.asyncio
async def test_me_exposes_permissions_roles_and_scope(client, db, headers):
    post = await make_post(db)
    other = await make_post(db)
    user = await make_user(db, perms=["tag.view", "observation.view"], post_id=post.id, extra_access={other.id: False})

    response = await client.get(ME, headers=headers(user))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == user.id
    assert body["is_admin"] is False
    assert body["permissions"] == ["observation.view", "tag.view"]
    assert body["post_id"] == post.id
    assert body["accessible_post_ids"] == sorted([post.id, other.id])
    assert len(body["roles"]) == 1
    assert set(body["roles"][0]) == {"id", "name", "display_name"}


@pytest.mark.asyncio
async def test_me_for_admin_with_post_access_all(client, db, headers):
    admin = await make_admin(db)
    body = (await client.get(ME, headers=headers(admin))).json()
    assert body["is_admin"] is True
    assert "post.access_all" in body["permissions"]
    assert "observation.manage_any" in body["permissions"]
    assert "media.manage_any" in body["permissions"]
    assert body["accessible_post_ids"] is None
    assert body["roles"][0]["name"] == "admin"


@pytest.mark.asyncio
async def test_refresh_token_is_rejected_as_bearer(client, db):
    user = await make_user(db)
    refresh = AuthService.create_refresh_token(user.id)
    response = await client.get(ME, headers={"Authorization": f"Bearer {refresh}"})
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_token"


@pytest.mark.asyncio
async def test_access_token_without_session_is_refused(client, db):
    """The login always binds a token to a session; one without sid would skip every session and certificate check"""
    user = await make_user(db)
    response = await client.get(ME, headers={"Authorization": f"Bearer {AuthService.create_access_token(user.id)}"})
    assert response.status_code == 401
    assert response.json()["code"] == "invalid_token"


@pytest.mark.asyncio
async def test_missing_credentials(client, db):
    response = await client.get(ME)
    assert response.status_code == 401
    assert response.json()["code"] == "credentials_required"


@pytest.mark.asyncio
async def test_check_permission(client, db, headers):
    user = await make_user(db, perms=["tag.view"])
    ok = await client.post(CHECK, json={"all": ["tag.view"]}, headers=headers(user))
    assert ok.status_code == 200, ok.text
    assert ok.json() == {"allowed": True, "missing": []}

    denied = await client.post(CHECK, json={"all": ["tag.view", "tag.archive"]}, headers=headers(user))
    assert denied.json() == {"allowed": False, "missing": ["tag.archive"]}

    unknown = await client.post(CHECK, json={"all": ["does.not.exist"]}, headers=headers(user))
    assert unknown.status_code == 422


@pytest.mark.asyncio
async def test_403_body_is_structured(client, db, headers):
    from app.backend.enums.routes import TagRoutes
    user = await make_user(db, perms=["tag.view"])
    response = await client.post(V1.path(TagRoutes.CREATE), json={"name": "x"}, headers=headers(user))
    assert response.status_code == 403
    detail = response.json()
    assert detail["code"] == "permission_denied"
    assert detail["required"] == {"all": ["tag.view", "tag.create"], "any": []}
    assert detail["missing"] == ["tag.create"]
    assert detail["title"] == "Insufficient permissions" and detail["status"] == 403
