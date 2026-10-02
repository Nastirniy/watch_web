"""Registered routes and the route enums match one to one, no route hides another, versions stay apart."""
import ast
import inspect
import re
from enum import Enum
from pathlib import Path

from fastapi.routing import APIRoute
from starlette.routing import Route

from app.backend.api.versioning import PublishedVersion
from app.backend.enums import routes as route_enums
from app.backend.enums.routes import APIVersionRoutes, DocsRoutes, Routes, SystemRoutes
from app.main import fastapi_app

PARAM = re.compile(r"{[^}]+}")
API_DIR = Path(__file__).resolve().parent.parent / "app" / "backend" / "api"
VERSION_FOLDER = re.compile(r"v\d+")
# Groups outside any version: their values are full paths
UNVERSIONED = {SystemRoutes, DocsRoutes}


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


def _groups() -> list:
    """Route groups: the enums of enums/routes.py with a BASE member"""
    return [cls for _, cls in inspect.getmembers(route_enums, inspect.isclass)
            if issubclass(cls, Enum) and "BASE" in cls.__members__]


def _published_paths(member) -> set:
    """Full paths of a versioned member in every version that publishes it"""
    paths = set()
    for version in PublishedVersion.published.values():
        try:
            paths.add(version.path(member))
        except LookupError:
            pass
    return paths


def test_routes_lists_every_group():
    assert set(_groups()) == {value for value in vars(Routes).values() if value in _groups()}
    assert Routes.API_VERSION is APIVersionRoutes


def test_every_enum_member_is_registered():
    registered = _registered_paths()
    unused = []
    for group in _groups():
        for member in group:
            if member is group.BASE:
                continue
            paths = {member.value} if group in UNVERSIONED else _published_paths(member)
            if not paths & registered:
                unused.append(f"{group.__name__}.{member.name}")
    assert not unused, f"enum members without a route (remove them or register the route): {unused}"


def test_every_route_is_an_enum_member():
    declared = set()
    for group in _groups():
        for member in group:
            if member is not group.BASE:
                declared |= {member.value} if group in UNVERSIONED else _published_paths(member)
    literal = _registered_paths() - declared
    assert not literal, f"routes not declared in enums/routes.py: {sorted(literal)}"


def _registered_paths() -> set:
    """Every path the app answers, /docs and /openapi.json included"""
    return {route.path for route in fastapi_app.routes if isinstance(route, Route)}


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
