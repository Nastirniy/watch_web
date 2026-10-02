"""Registered routes and the route enums match one to one, and no route hides another."""
import re

from fastapi.routing import APIRoute
from starlette.routing import Route

from app.backend.enums.routes import APIVersionRoutes, RouteEnum, Routes, VersionedRouteEnum
from app.main import fastapi_app

PARAM = re.compile(r"{[^}]+}")


def _groups():
    found, pending = [], list(RouteEnum.__subclasses__())
    while pending:
        cls = pending.pop()
        if cls is VersionedRouteEnum:
            pending.extend(cls.__subclasses__())
        else:
            found.append(cls)
    return found


def _enum_paths() -> set:
    paths = set()
    for group in _groups():
        for member in group:
            if member is group.BASE:
                continue
            if issubclass(group, VersionedRouteEnum):
                paths |= {member.versioned_path(version) for version in APIVersionRoutes}
            else:
                paths.add(member.path())
    return paths


def _registered_paths() -> set:
    """Every path the app answers, /docs and /openapi.json included"""
    return {route.path for route in fastapi_app.routes if isinstance(route, Route)}


def test_routes_lists_every_group():
    assert set(_groups()) == {value for value in vars(Routes).values() if isinstance(value, type) and issubclass(value, RouteEnum)}


def test_every_enum_member_is_registered():
    unused = _enum_paths() - _registered_paths()
    assert not unused, f"enum members without a route (remove them or register the route): {sorted(unused)}"


def test_every_route_is_an_enum_member():
    literal = _registered_paths() - _enum_paths()
    assert not literal, f"routes not declared in enums/routes.py: {sorted(literal)}"


def test_no_route_is_shadowed_by_an_earlier_one():
    """Starlette takes the first match: /{user_id} registered before /filter-schema swallows it"""
    routes = [route for route in fastapi_app.routes if isinstance(route, APIRoute)]
    shadowed = []
    for index, later in enumerate(routes):
        sample = PARAM.sub("sample-value", later.path_format)
        for earlier in routes[:index]:
            if earlier.path_format != later.path_format and earlier.methods & later.methods and earlier.path_regex.match(sample):
                shadowed.append(f"{sorted(later.methods)} {later.path_format} hidden by {earlier.path_format}")
    assert not shadowed, shadowed
