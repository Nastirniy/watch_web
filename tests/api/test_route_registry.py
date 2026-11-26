"""Registered routes and the route enums match one to one, no route hides another, versions stay apart."""
import ast
import inspect
import re
from enum import Enum
from pathlib import Path

from fastapi.routing import APIRoute
from starlette.routing import Route

from tests.paths import BACKEND_DIR
from app.backend.api.versioning import PublishedVersion
from app.backend.enums import routes as route_enums
from app.backend.enums.routes import APIVersionRoutes, Routes, SystemRoutes
from app.main import fastapi_app

PARAM = re.compile(r"{[^}]+}")
API_DIR = BACKEND_DIR / "api"
VERSION_FOLDER = re.compile(r"v\d+")
# Groups outside any version: their values are full paths (DocsRoutes is relative to each version)
UNVERSIONED = {SystemRoutes}


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


APP_DIR = API_DIR.parent.parent
VERSION_MODULE = re.compile(r"app\.backend\.api\.v\d+(\.|$)")
API_MODULE = re.compile(r"app\.backend\.api(\.|$)")
# Wire the routers into the app and write the contract: the only modules outside api/ that know it
API_WIRING = {APP_DIR / "main.py", APP_DIR / "backend" / "cli" / "api_contract.py"}


def _imported_modules(path: Path, root: Path = APP_DIR.parent):
    """Absolute names a module imports: relative imports resolved against its package, and for
    `from package import name` also package.name (the name may be a module: from app.backend.api import v1)"""
    package = list(path.relative_to(root).with_suffix("").parts[:-1])
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
        if isinstance(node, ast.ImportFrom):
            base = package[:len(package) - node.level + 1] if node.level else []
            module = ".".join(base + ([node.module] if node.module else []))
            yield node.lineno, module
            for alias in node.names:
                yield node.lineno, f"{module}.{alias.name}"
        elif isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name


def test_imported_modules_resolve_relative_and_package_imports(tmp_path):
    """The import rules see every form: relative imports and a module imported as a name of its package"""
    module = tmp_path / "app" / "backend" / "api" / "shared" / "x.py"
    module.parent.mkdir(parents=True)
    module.write_text("from app.backend.api import v1\nfrom ..v2.models import y\nfrom .. import v3\n"
                      "from app.backend import api\n", encoding="utf-8")
    names = {name for _, name in _imported_modules(module, root=tmp_path)}
    assert {"app.backend.api.v1", "app.backend.api.v2.models", "app.backend.api.v3", "app.backend.api"} <= names


def test_shared_and_system_never_import_a_version():
    """api/shared serves every version and api/system none: neither may depend on what one version publishes"""
    offenders = [f"{path.relative_to(API_DIR)}:{line} imports {name}"
                 for folder in ("shared", "system") for path in sorted((API_DIR / folder).rglob("*.py"))
                 for line, name in _imported_modules(path) if VERSION_MODULE.match(name)]
    assert not offenders, offenders


def test_no_model_is_defined_in_api_shared():
    """Request and response models are the contract of a version and live in api/vN/models"""
    import importlib
    from pydantic import BaseModel

    offenders = []
    for path in sorted((API_DIR / "shared").rglob("*.py")):
        module = importlib.import_module(".".join(path.relative_to(APP_DIR.parent).with_suffix("").parts))
        offenders += [f"{module.__name__}.{name}" for name, value in vars(module).items()
                      if inspect.isclass(value) and issubclass(value, BaseModel) and value.__module__ == module.__name__]
    assert not offenders, offenders


def test_nothing_outside_api_imports_api():
    """Services, filters, models and workers serve every version: they never depend on API models or routes"""
    offenders = [f"{path.relative_to(APP_DIR.parent)}:{line} imports {name}"
                 for path in sorted(APP_DIR.rglob("*.py"))
                 if API_DIR not in path.parents and path not in API_WIRING
                 for line, name in _imported_modules(path) if API_MODULE.match(name)]
    assert not offenders, offenders


# Code that runs without a request; HTTP lives in api/, auth/ (require, dependencies, ...) and errors/ (but coded.py)
FRAMEWORK_FREE = ("services", "filters", "models", "workers", "enums", "bootstrap", "utils", "pki")
FRAMEWORK = re.compile(r"(fastapi|starlette)(\.|$)")


def _module_file(name: str):
    """File of an app module or package, None for anything else (third-party, or a name inside a module)"""
    base = APP_DIR.parent.joinpath(*name.split("."))
    return next((path for path in (base.with_suffix(".py"), base / "__init__.py") if path.is_file()), None)


def test_domain_code_never_reaches_the_web_framework():
    """Services, filters, models, workers, enums, seeding and utils import neither FastAPI nor Starlette, directly
    or through another app module: they raise CodedError (errors/coded.py) and the route answers
    ApiError.from_coded(exc)"""
    offenders = {}
    for folder in FRAMEWORK_FREE:
        for start in sorted((APP_DIR / "backend" / folder).rglob("*.py")):
            chains, seen = [(start, (start.relative_to(APP_DIR.parent).as_posix(),))], {start}
            while chains and start not in offenders:
                path, chain = chains.pop(0)
                for _, name in _imported_modules(path):
                    if FRAMEWORK.match(name):
                        offenders.setdefault(start, " -> ".join(chain + (name,)))
                    elif name.startswith("app.") and (target := _module_file(name)) and target not in seen:
                        seen.add(target)
                        chains.append((target, chain + (name,)))
    assert not offenders, "\n".join(offenders.values())


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
    """Every path the app answers, the documents and viewers of each version included"""
    return {route.path for route in fastapi_app.routes if isinstance(route, Route)}


def test_no_route_is_shadowed_by_an_earlier_one():
    """Starlette takes the first match: /{tag_id} registered before /list-all swallows it"""
    routes = [route for route in fastapi_app.routes if isinstance(route, APIRoute)]
    shadowed = []
    for index, later in enumerate(routes):
        sample = PARAM.sub("sample-value", later.path_format)
        for earlier in routes[:index]:
            if earlier.path_format != later.path_format and earlier.methods & later.methods and earlier.path_regex.match(sample):
                shadowed.append(f"{sorted(later.methods)} {later.path_format} hidden by {earlier.path_format}")
    assert not shadowed, shadowed
