"""Route access map: generated from route metadata, filtered per caller."""
import pytest
from fastapi.routing import APIRoute

from app.main import fastapi_app
from app.backend.enums.routes import APIVersionRoutes, AuthRoutes
from tests.helpers import make_user, make_admin

ROUTE_ACCESS = AuthRoutes.ROUTE_ACCESS.versioned_path()
PUBLIC = {member.versioned_path(version) for version in APIVersionRoutes for member in (AuthRoutes.LOGIN, AuthRoutes.REFRESH)}
API_PREFIXES = tuple(version.value + "/" for version in APIVersionRoutes)


def test_every_api_route_is_guarded():
    guarded = {(entry.method, entry.path) for entry in fastapi_app.state.route_access_map}
    missing = []
    for route in fastapi_app.routes:
        if not isinstance(route, APIRoute) or not route.path.startswith(API_PREFIXES):
            continue
        if route.path in PUBLIC:
            continue
        for method in route.methods:
            if (method, route.path) not in guarded:
                missing.append((method, route.path))
    assert not missing, f"routes without require(): {missing}"


@pytest.mark.asyncio
async def test_admin_sees_every_guarded_route(client, db, headers):
    admin = await make_admin(db)
    response = await client.get(ROUTE_ACCESS, headers=headers(admin))
    assert response.status_code == 200, response.text
    routes = response.json()["routes"]
    assert len(routes) == len(fastapi_app.state.route_access_map)
    sample = routes[0]
    assert set(sample) == {"method", "path", "name", "tags", "required"}
    assert set(sample["required"]) == {"all", "any", "admin"}


@pytest.mark.asyncio
async def test_user_without_permissions_sees_only_authenticated_routes(client, db, headers):
    user = await make_user(db)
    routes = (await client.get(ROUTE_ACCESS, headers=headers(user))).json()["routes"]
    assert routes, "authenticated-only routes must be listed"
    for route in routes:
        assert route["required"] == {"all": [], "any": [], "admin": False}, route
    paths = {r["path"] for r in routes}
    assert AuthRoutes.ME.versioned_path() in paths
    assert ROUTE_ACCESS in paths


@pytest.mark.asyncio
async def test_map_hides_forbidden_routes(client, db, headers):
    user = await make_user(db, perms=["tag.view"])
    routes = (await client.get(ROUTE_ACCESS, headers=headers(user))).json()["routes"]
    listed = {(r["method"], r["path"]) for r in routes}
    from app.backend.enums.routes import TagRoutes, RoleRoutes
    assert ("GET", TagRoutes.LIST_ALL.versioned_path()) in listed
    assert ("POST", TagRoutes.CREATE.versioned_path()) not in listed
    assert not any(path.startswith(RoleRoutes.BASE.versioned_path()) for _, path in listed)
