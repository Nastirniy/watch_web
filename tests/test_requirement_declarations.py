"""The declaration of a route and what its handler checks are the same (finding L8, decision 2026-10-30).

GET /posts/{id}/observation-stats needs post.view and observation.view at the post. POST /media/posters/reconcile acts
on every post, and the system-assignment writes grant at the root: they declare it with require(at_root=[...]), so
GET /auth/route-access lists them only for callers who can act and a brigade administrator gets the 403 of the gate."""
from app.backend.api.v1 import V1
from app.backend.auth.context import AuthContext
from app.backend.auth.require import Require, require
from app.backend.auth.scope import Grant, Node, ROOT
from app.backend.config import settings
from app.backend.enums.general import BuiltinRole, Permissions as P, UserType
from app.backend.enums.routes import AuthRoutes, MediaRoutes, PostRoutes, UserRoutes
from tests.helpers import builtin_role, give, make_admin, make_group, make_post, make_user

ROUTE_ACCESS = V1.path(AuthRoutes.ROUTE_ACCESS)
RECONCILE = V1.path(MediaRoutes.POSTERS_RECONCILE)
STATS = V1.path(PostRoutes.POST_OBSERVATION_STATS)
SYSTEM_ASSIGNMENTS = V1.path(UserRoutes.USER_SYSTEM_ASSIGNMENTS)
SYSTEM_ASSIGNMENT = V1.path(UserRoutes.USER_SYSTEM_ASSIGNMENT_BY_ID)


def _grant(node: Node, *perms: str) -> Grant:
    return Grant(1, "role", "Role", False, node, frozenset(perms))


async def _routes(client, headers, user) -> set:
    body = (await client.get(ROUTE_ACCESS, headers=headers(user))).json()
    return {(route["method"], route["path"]) for route in body["routes"]}


def test_at_root_is_held_at_the_root_only():
    spec = Require.of(all=["a.view"], at_root=["a.update"])
    assert spec.as_dict() == {"all": ["a.view"], "any": [], "at_root": ["a.update"]}
    at_root = AuthContext(user=None, grants=(_grant(ROOT, "a.view", "a.update"),))
    assert spec.evaluate(at_root).allowed
    below = AuthContext(user=None, grants=(_grant(Node.group(1), "a.view", "a.update"),))
    result = spec.evaluate(below)
    assert not result.allowed and result.missing == ("a.update",)


def test_require_takes_at_root():
    assert require(at_root=[P.USER_UPDATE]).at_root == ("user.update",)


async def test_observation_stats_need_post_view_and_observation_view_at_the_post(client, db, headers):
    with_view, with_observations = await make_post(db), await make_post(db)
    user = await make_user(db, post_id=with_view.id, user_type=UserType.VIEWER)
    await give(db, user, [P.POST_VIEW], Node.post(with_view.id))
    await give(db, user, [P.OBSERVATION_VIEW], Node.post(with_observations.id))

    for post, missing in ((with_view, "observation.view"), (with_observations, "post.view")):
        response = await client.get(STATS.format(post_id=post.id), headers=headers(user))
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "post_access_denied" and response.json()["missing"] == [missing]

    both = await make_post(db)
    await give(db, user, [P.POST_VIEW, P.OBSERVATION_VIEW], Node.post(both.id))
    assert (await client.get(STATS.format(post_id=both.id), headers=headers(user))).status_code == 200


async def test_observation_stats_scope_comes_before_existence(client, db, headers):
    post = await make_post(db)
    user = await make_user(db, perms=[P.POST_VIEW, P.OBSERVATION_VIEW], post_id=post.id)
    response = await client.get(STATS.format(post_id=999999), headers=headers(user))
    assert response.status_code == 403 and response.json()["code"] == "post_access_denied", response.text


async def test_poster_reconcile_needs_all_three_at_the_root(client, db, headers, tmp_path, monkeypatch):
    monkeypatch.setattr(settings.storage, "upload_path", str(tmp_path))
    east = await make_group(db, "East")
    user = await make_user(db, user_type=UserType.SYSTEM_ADMINISTRATOR)
    await give(db, user, [P.MEDIA_VIEW, P.MEDIA_UPDATE], Node.group(east.id))
    await give(db, user, [P.MEDIA_MONITOR], ROOT)

    response = await client.post(RECONCILE, json={}, headers=headers(user))
    assert response.status_code == 403, response.text
    body = response.json()
    assert body["code"] == "permission_denied" and body["missing"] == ["media.view", "media.update"]
    assert body["required"] == {"all": [], "any": [], "at_root": ["media.view", "media.update", "media.monitor"]}
    assert ("POST", RECONCILE) not in await _routes(client, headers, user)

    await give(db, user, [P.MEDIA_VIEW, P.MEDIA_UPDATE], ROOT)
    assert ("POST", RECONCILE) in await _routes(client, headers, user)
    assert (await client.post(RECONCILE, json={}, headers=headers(user))).status_code == 200


async def test_a_brigade_administrator_sees_and_gets_no_system_assignment_write(client, db, headers):
    brigade = await make_group(db, "Brigade")
    post = await make_post(db, group_id=brigade.id)
    caller = await make_user(db, post_id=post.id, user_type=UserType.ADMINISTRATOR)
    await give(db, caller, [P.USER_VIEW, P.USER_UPDATE], Node.group(brigade.id))
    target = await make_user(db, post_id=post.id)
    role = await builtin_role(db, BuiltinRole.AUDITOR)

    routes = await _routes(client, headers, caller)
    assert ("POST", SYSTEM_ASSIGNMENTS) not in routes and ("DELETE", SYSTEM_ASSIGNMENT) not in routes
    assert ("GET", SYSTEM_ASSIGNMENTS) in routes     # a read, of the users the caller sees: no root needed

    response = await client.post(SYSTEM_ASSIGNMENTS.format(user_id=target.id), headers=headers(caller),
                                 json={"role_id": role.id})
    assert response.status_code == 403, response.text
    body = response.json()
    assert body["code"] == "permission_denied" and body["missing"] == ["user.update"]
    assert body["required"] == {"all": ["user.view"], "any": [], "at_root": ["user.update"]}
    response = await client.delete(SYSTEM_ASSIGNMENT.format(user_id=target.id, assignment_id=1), headers=headers(caller))
    assert response.status_code == 403 and response.json()["code"] == "permission_denied", response.text
    assert (await client.get(SYSTEM_ASSIGNMENTS.format(user_id=target.id), headers=headers(caller))).status_code == 200


async def test_the_root_holder_keeps_the_system_assignment_routes(client, db, headers):
    admin = await make_admin(db)
    target = await make_user(db, user_type=UserType.SYSTEM_ADMINISTRATOR)
    role = await builtin_role(db, BuiltinRole.AUDITOR)
    routes = await _routes(client, headers, admin)
    assert ("POST", SYSTEM_ASSIGNMENTS) in routes and ("DELETE", SYSTEM_ASSIGNMENT) in routes
    response = await client.post(SYSTEM_ASSIGNMENTS.format(user_id=target.id), headers=headers(admin),
                                 json={"role_id": role.id})
    assert response.status_code == 201, response.text
