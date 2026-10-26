"""Contract of /auth/me, the login response, /auth/check-permission and token handling.

The auth fields are what the web client builds its interface from: pages by user_type, the menu by permissions
(held anywhere), buttons by root_permissions plus the entry of the post in post_permissions."""
from dataclasses import dataclass

import pytest
from sqlmodel import select

from app.backend.enums.general import Permissions, UserType
from app.backend.enums.routes import AuthRoutes, UserRoutes
from app.backend.api.v1 import V1
from app.backend.auth.scope import Node
from app.backend.auth.service import AuthService
from app.backend.auth.user_types import USER_TYPES
from app.backend.models.post import Post
from app.backend.models.role import Role
from app.backend.models.scope_group import ScopeGroup
from app.main import fastapi_app
from tests.helpers import assign, make_admin, make_group, make_post, make_role, make_user
from tests.test_sessions import make_session

ME = V1.path(AuthRoutes.ME)
CHECK = V1.path(AuthRoutes.CHECK_PERMISSION)
REFRESH = V1.path(AuthRoutes.REFRESH)
AUTH_FIELDS = ("post_id", "user_type", "permissions", "assignments", "root_permissions", "post_permissions")
VIEW = sorted(USER_TYPES[UserType.VIEWER].base_permissions)
OPERATOR = sorted(USER_TYPES[UserType.OPERATOR].base_permissions)


@dataclass
class Tree:
    east: ScopeGroup
    kharkiv: ScopeGroup
    west: ScopeGroup
    a: Post
    a2: Post
    b: Post


@pytest.fixture
async def tree(db) -> Tree:
    """East > Kharkiv > posts A, A2; West > post B"""
    east = await make_group(db, "East")
    kharkiv = await make_group(db, "Kharkiv", parent_id=east.id)
    west = await make_group(db, "West")
    return Tree(east, kharkiv, west, await make_post(db, "A", group_id=kharkiv.id),
                await make_post(db, "A2", group_id=kharkiv.id), await make_post(db, "B", group_id=west.id))


async def builtin(db, user_type: UserType) -> Role:
    return (await db.exec(select(Role).where(Role.builtin_key == user_type.value))).one()


async def operator_of_kharkiv(db, tree: Tree):
    """An operator with the built-in operator role at Kharkiv (posts A, A2) and the viewer role at post B"""
    user = await make_user(db, post_id=tree.a.id, user_type=UserType.OPERATOR)
    at_group = await assign(db, user, await builtin(db, UserType.OPERATOR), Node.group(tree.kharkiv.id))
    at_b = await assign(db, user, await builtin(db, UserType.VIEWER), Node.post(tree.b.id))
    return user, at_group, at_b


async def _archive(db, row) -> None:
    row.is_deleted = True
    db.add(row)
    await db.commit()


@pytest.mark.asyncio
async def test_me_carries_type_and_post_permissions(client, db, headers, tree):
    user, at_group, at_b = await operator_of_kharkiv(db, tree)

    response = await client.get(ME, headers=headers(user))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == user.id and body["post_id"] == tree.a.id
    assert body["user_type"] == "operator"
    for removed in ("is_admin", "accessible_post_ids", "roles"):
        assert removed not in body
    assert body["permissions"] == OPERATOR            # held anywhere: the menu
    assert body["root_permissions"] == []
    assert body["post_permissions"] == {str(tree.a.id): OPERATOR, str(tree.a2.id): OPERATOR, str(tree.b.id): VIEW}
    assert "observation.update" in body["post_permissions"][str(tree.a.id)]
    assert "observation.update" not in body["post_permissions"][str(tree.b.id)]

    # The same list GET /users/{user_id}/assignments returns
    assert [item["id"] for item in body["assignments"]] == [at_group.id, at_b.id]
    assert body["assignments"][0]["scope"] == {"kind": "group", "id": tree.kharkiv.id, "name": "Kharkiv"}
    assert body["assignments"][1]["role"]["name"] == "viewer" and body["assignments"][1]["role"]["is_builtin"] is True
    own = await client.get(V1.path(UserRoutes.USER_ASSIGNMENTS, user_id=user.id), headers=headers(await make_admin(db)))
    assert body["assignments"] == own.json()


@pytest.mark.asyncio
async def test_post_permissions_follow_the_evaluator(client, db, headers, tree):
    """A grant on an archived post stops applying there, a group grant keeps applying to its archived posts;
    an assignment of an archived role is listed (role.is_deleted) and grants nothing"""
    user, at_group, at_b = await operator_of_kharkiv(db, tree)
    stats = await make_role(db, ["statistics.view"])
    archived_role = await assign(db, user, stats, Node.post(tree.a.id))
    await _archive(db, stats)
    await _archive(db, tree.a2)
    await _archive(db, tree.b)

    body = (await client.get(ME, headers=headers(user))).json()
    assert body["post_permissions"] == {str(tree.a.id): OPERATOR, str(tree.a2.id): OPERATOR}
    assert body["permissions"] == OPERATOR
    assert [(item["id"], item["role"]["is_deleted"]) for item in body["assignments"]] == [
        (at_group.id, False), (at_b.id, False), (archived_role.id, True)]


@pytest.mark.asyncio
async def test_a_grant_at_an_archived_group_puts_nothing_in_effect(client, db, headers, tree):
    """An assignment at an archived group applies nowhere (its posts keep what the live groups above them grant)"""
    user = await make_user(db, post_id=tree.a.id, user_type=UserType.OPERATOR)
    await assign(db, user, await builtin(db, UserType.OPERATOR), Node.group(tree.kharkiv.id))
    await assign(db, user, await builtin(db, UserType.VIEWER), Node.group(tree.east.id))
    await assign(db, user, await builtin(db, UserType.VIEWER), Node.group(tree.west.id))
    await _archive(db, tree.kharkiv)
    await _archive(db, tree.west)

    login = headers(user)
    body = (await client.get(ME, headers=login)).json()
    assert body["post_permissions"] == {str(tree.a.id): VIEW, str(tree.a2.id): VIEW}
    check =await client.post(CHECK, json={"all": ["observation.update"], "post_id": tree.a.id}, headers=login)
    assert check.json() == {"allowed": False, "missing": ["observation.update"]}


@pytest.mark.asyncio
async def test_me_for_the_admin_at_the_root(client, db, headers):
    admin = await make_admin(db)
    body = (await client.get(ME, headers=headers(admin))).json()
    every = sorted(p.value for p in Permissions)
    assert body["user_type"] == "system_administrator"
    assert body["permissions"] == every and set(body["root_permissions"]) | set(body["system_permissions"]) == set(every)
    assert "post.access_all" not in body["permissions"] and "admin.true" not in body["permissions"]
    assert body["post_permissions"] == {}
    (assignment,) = body["assignments"]
    assert assignment["role"]["name"] == "administrator" and assignment["scope"]["kind"] == "root"


@pytest.mark.asyncio
async def test_login_response_carries_the_same_auth_fields(client, db, headers, tree):
    user, _, _ = await operator_of_kharkiv(db, tree)
    session_row = await make_session(db, user)
    refreshed = await client.post(REFRESH, json={"refresh_token": AuthService.create_refresh_token(user.id, session_id=session_row.id)})
    assert refreshed.status_code == 200, refreshed.text
    me = (await client.get(ME, headers=headers(user))).json()
    assert {name: refreshed.json()[name] for name in AUTH_FIELDS} == {name: me[name] for name in AUTH_FIELDS}
    for removed in ("is_admin", "accessible_post_ids", "roles"):
        assert removed not in refreshed.json()


def test_auth_fields_are_required_in_the_schema():
    """Always present: openapi-typescript types them as non-optional"""
    schemas = V1.openapi(fastapi_app)["components"]["schemas"]
    for name in ("UserInfoResponse", "LoginResponse"):
        properties, required = schemas[name]["properties"], set(schemas[name]["required"])
        assert set(AUTH_FIELDS) <= required, name
        assert not {"is_admin", "accessible_post_ids", "roles"} & set(properties), name
    assert "RoleBriefResponse" not in schemas
    check = schemas["PermissionCheckRequest"]["properties"]["post_id"]
    assert {"type": "integer", "exclusiveMinimum": 0} in check["anyOf"]


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
async def test_check_permission_at_post(client, db, headers, tree):
    """With post_id: allowed when every permission of all (and one of any) is in effect at that post"""
    user, _, _ = await operator_of_kharkiv(db, tree)
    login = headers(user)

    async def check(**body) -> dict:
        response = await client.post(CHECK, json=body, headers=login)
        assert response.status_code == 200, response.text
        return response.json()

    assert await check(all=["observation.update"], post_id=tree.a.id) == {"allowed": True, "missing": []}
    assert await check(all=["observation.update"], post_id=tree.b.id) == {
        "allowed": False, "missing": ["observation.update"]}
    assert await check(all=["observation.update"]) == {"allowed": True, "missing": []}   # held anywhere
    assert await check(all=["observation.view"], any=["observation.update", "media.create"], post_id=tree.b.id) == {
        "allowed": False, "missing": ["observation.update", "media.create"]}
    assert (await check(all=["observation.view"], post_id=999999))["allowed"] is False   # no grant reaches it

    await _archive(db, tree.b)
    assert (await check(all=["observation.view"], post_id=tree.b.id))["allowed"] is False
    for invalid in (0, -1, "x"):
        refused = await client.post(CHECK, json={"all": ["observation.view"], "post_id": invalid}, headers=login)
        assert refused.status_code == 422, invalid


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
