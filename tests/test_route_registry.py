"""Registered routes and the route enums match one to one, no route hides another, versions stay apart."""
import ast
import re
from pathlib import Path

from fastapi.routing import APIRoute
from starlette.routing import Route

from app.backend.enums.routes import APIVersionRoutes, RouteEnum, Routes, VersionedRouteEnum
from app.main import fastapi_app

PARAM = re.compile(r"{[^}]+}")
API_DIR = Path(__file__).resolve().parent.parent / "app" / "backend" / "api"
VERSION_FOLDER = re.compile(r"v\d+")


def test_versions_never_import_each_other():
    """A version publishes only its own folder: shared code lives in api/shared or services"""
    offenders = []
    for folder in sorted(p for p in API_DIR.iterdir() if p.is_dir() and VERSION_FOLDER.fullmatch(p.name)):
        own = f"app.backend.api.{folder.name}"
        for path in sorted(folder.rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
                if isinstance(node, ast.ImportFrom) and node.level:
                    offenders.append(f"{path.relative_to(API_DIR)}:{node.lineno} relative import")
                    continue
                names = [node.module] if isinstance(node, ast.ImportFrom) else \
                    [alias.name for alias in node.names] if isinstance(node, ast.Import) else []
                for name in names:
                    if re.match(r"app\.backend\.api\.v\d+", name or "") and not (name == own or name.startswith(own + ".")):
                        offenders.append(f"{path.relative_to(API_DIR)}:{node.lineno} imports {name}")
    assert not offenders, offenders


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
