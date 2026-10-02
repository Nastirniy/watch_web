"""OpenAPI documentation of the problems (RFC 9457) every route can answer.

The problems of a route are found statically: the endpoint, its FastAPI dependencies and every app
function they call (transitively) are scanned for references to ErrorCode.X / AuthErrorCode.X /
AccessErrorCode.X. A route whose request FastAPI validates also answers validation_error, and every
route can answer internal_error. Nothing is declared by hand, so the documentation cannot drift from
the code; tests/test_openapi.py checks the result.

Each problem type becomes a component schema (typed params, const code / title / status) and each
response status lists its problem types as a oneOf with a discriminator on "code", so generators such
as openapi-typescript produce a discriminated union per status.
"""
import ast
import inspect
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Literal, Optional, Set, Tuple

from fastapi import FastAPI
from fastapi.dependencies.utils import get_flat_dependant
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, create_model

from app.backend.enums.errors import (
    PROBLEM_ENUMS, AccessErrorCode, ErrorCode, FieldErrorCode, ProblemType, RequiredPermissions, UserRef,
)
from app.backend.enums.routes import APIVersionRoutes
from app.backend.services.api_errors import PROBLEM_JSON, is_optional
from app.backend.services.permissions import REQUIRE_ATTR

APP_PACKAGE = "app.backend"
APP_DIR = Path(__file__).resolve().parent
CODE_ENUM_NAMES = {cls.__name__: cls for cls in PROBLEM_ENUMS}
# Problems of every route: FastAPI's own validation is added only when the route has inputs
ALWAYS = (ErrorCode.INTERNAL_ERROR,)

FunctionKey = Tuple[str, str]  # (module, qualname)


# ============================================================ call graph

class _Module:
    def __init__(self, name: str, tree: ast.Module):
        self.name = name
        self.functions: Dict[str, ast.AST] = {}
        self.classes: Set[str] = set()
        self.imports: Dict[str, str] = {}   # local name -> "module" or "module:attr"
        self._index(tree)

    def _index(self, tree: ast.Module) -> None:
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                for alias in node.names:
                    self.imports[alias.asname or alias.name] = f"{node.module}:{alias.name}"
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    self.imports[alias.asname or alias.name.split(".")[0]] = alias.name if alias.asname else alias.name.split(".")[0]

        def visit(body: Iterable[ast.AST], prefix: str) -> None:
            for node in body:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    qualname = f"{prefix}{node.name}"
                    self.functions[qualname] = node
                    visit(node.body, f"{qualname}.<locals>.")
                elif isinstance(node, ast.ClassDef):
                    self.classes.add(f"{prefix}{node.name}")
                    visit(node.body, f"{prefix}{node.name}.")

        visit(tree.body, "")


class CallGraph:
    """Problem types reachable from a function of the app"""

    def __init__(self, package_dir: Path = APP_DIR, package: str = APP_PACKAGE):
        self.modules: Dict[str, _Module] = {}
        for path in sorted(package_dir.rglob("*.py")):
            rel = path.relative_to(package_dir)
            if "migrations" in rel.parts:
                continue
            name = ".".join((package, *rel.with_suffix("").parts)).removesuffix(".__init__")
            self.modules[name] = _Module(name, ast.parse(path.read_text(encoding="utf-8")))
        # Methods by bare name, for calls on values whose class is not known statically
        self.methods_by_name: Dict[str, List[FunctionKey]] = {}
        for module in self.modules.values():
            for qualname in module.functions:
                parts = qualname.split(".")
                if len(parts) == 2 and parts[0] in module.classes:
                    self.methods_by_name.setdefault(parts[1], []).append((module.name, qualname))
        self._memo: Dict[FunctionKey, Set[ProblemType]] = {}

    # ---------------------------------------------------------------- resolution
    def _imported(self, module: _Module, name: str) -> Optional[Tuple[str, Optional[str]]]:
        """(module, attribute) a local name refers to; attribute None for a module"""
        target = module.imports.get(name)
        if target is None:
            return None
        if ":" in target:
            source, attr = target.split(":")
            submodule = f"{source}.{attr}"
            return (submodule, None) if submodule in self.modules else (source, attr)
        return target, None

    def _resolve(self, module: _Module, func: ast.AST, owner_class: Optional[str]) -> List[FunctionKey]:
        if isinstance(func, ast.Name):
            if func.id in module.functions:
                return [(module.name, func.id)]
            imported = self._imported(module, func.id)
            if imported and imported[1] and imported[1] in self.modules.get(imported[0], _EMPTY).functions:
                return [(imported[0], imported[1])]
            return []
        if not isinstance(func, ast.Attribute):
            return []
        attr = func.attr
        base = func.value
        if isinstance(base, ast.Name):
            if base.id in ("self", "cls") and owner_class:
                return [(module.name, f"{owner_class}.{attr}")]
            if base.id in module.classes:
                return [(module.name, f"{base.id}.{attr}")]
            imported = self._imported(module, base.id)
            if imported:
                source, name = imported
                if name is None:  # module.function
                    return [(source, attr)] if attr in self.modules.get(source, _EMPTY).functions else []
                return [(source, f"{name}.{attr}")]  # Class.method
        # A method called on a value (get_media_manager().store_file_from_path): only an unambiguous name
        candidates = self.methods_by_name.get(attr, [])
        return candidates if len(candidates) == 1 else []

    # ---------------------------------------------------------------- traversal
    def problems_of(self, key: FunctionKey, _stack: Optional[Set[FunctionKey]] = None) -> Set[ProblemType]:
        if key in self._memo:
            return self._memo[key]
        module = self.modules.get(key[0])
        node = module.functions.get(key[1]) if module else None
        if node is None:
            return set()
        stack = (_stack or set()) | {key}
        parts = key[1].split(".")
        owner_class = parts[-2] if len(parts) >= 2 and parts[-2] in module.classes else None
        found: Set[ProblemType] = set()
        # The body only: argument defaults hold Depends(require(...)), which the dependency tree covers precisely
        body = ast.Module(body=node.body, type_ignores=[])
        for child in ast.walk(body):
            if isinstance(child, ast.Attribute) and isinstance(child.value, ast.Name) and child.value.id in CODE_ENUM_NAMES:
                found.add(CODE_ENUM_NAMES[child.value.id][child.attr])
            elif isinstance(child, ast.Call):
                for callee in self._resolve(module, child.func, owner_class):
                    if callee not in stack:
                        found |= self.problems_of(callee, stack)
        self._memo[key] = found
        return found

    def problems_of_callable(self, func: Any) -> Set[ProblemType]:
        func = inspect.unwrap(func)
        module = getattr(func, "__module__", None)
        qualname = getattr(func, "__qualname__", None)
        if not module or not qualname or not module.startswith(APP_PACKAGE):
            return set()
        return self.problems_of((module, qualname))


class _Empty:
    functions: Dict[str, ast.AST] = {}


_EMPTY = _Empty()


def _dependency_calls(dependant) -> Iterable[Any]:
    for dependency in dependant.dependencies:
        if dependency.call is not None:
            yield dependency.call
        yield from _dependency_calls(dependency)


def route_validates(route: APIRoute) -> bool:
    """FastAPI validates path, query, header, cookie or body values of this route (its own or of a dependency)"""
    flat = get_flat_dependant(route.dependant)
    return bool(flat.path_params or flat.query_params or flat.header_params or flat.cookie_params or flat.body_params)


def _require_problems(spec) -> Set[ProblemType]:
    """The 403 of a require() dependency follows its spec: admin_required only on administration routes"""
    problems = set()
    if spec.all or spec.any:
        problems.add(AccessErrorCode.PERMISSION_DENIED)
    if spec.admin:
        problems.add(AccessErrorCode.ADMIN_REQUIRED)
    return problems


def route_problems(route: APIRoute, graph: CallGraph) -> Set[ProblemType]:
    problems = set(ALWAYS) | graph.problems_of_callable(route.endpoint)
    for call in _dependency_calls(route.dependant):
        spec = getattr(call, REQUIRE_ATTR, None)
        # The require() closure itself names both 403 codes; its sub-dependencies (authentication) are walked anyway
        problems |= _require_problems(spec) if spec is not None else graph.problems_of_callable(call)
    if route_validates(route):
        problems.add(ErrorCode.VALIDATION_ERROR)
    return problems


# ============================================================ schemas

def _camel(code: str) -> str:
    return "".join(part.capitalize() for part in code.split("_"))


def schema_name(code: ProblemType) -> str:
    return f"{_camel(code.value)}Problem"


class FieldError(BaseModel):
    """One invalid value of a validation_error. Codes of the API are listed in FieldErrorCode; built-in
    checks keep pydantic's codes (missing, string_too_short, int_parsing, json_invalid, ...). Other
    members are the params of the code (min_length, max_length, accepted, ...)."""
    model_config = ConfigDict(extra="allow")

    code: str = Field(description="Key of exactly this message in the localization package")
    detail: str = Field(description="English text of the message")
    pointer: Optional[str] = Field(default=None, description='JSON Pointer (RFC 6901) into the body, e.g. "#/username"')
    parameter: Optional[str] = Field(default=None, description="Name of the query, path, header or cookie parameter")
    location: Optional[Literal["query", "path", "header", "cookie"]] = Field(default=None, description="Where parameter is")


_PARAM_TYPES = {"errors": List[FieldError]}


def _problem_model(code: ProblemType) -> type:
    fields: Dict[str, Any] = {
        "type": (str, Field(description="URI of the problem type; dereference it for its documentation",
                            json_schema_extra={"format": "uri"})),
        "title": (Literal[code.title], Field(description="Summary of the problem type")),  # type: ignore[valid-type]
        "status": (Literal[code.status], Field(description="HTTP status code")),  # type: ignore[valid-type]
        "detail": (str, Field(description="Explanation of this occurrence", examples=[code.detail_template])),
        "instance": (str, Field(description="Path of the request that failed")),
        "code": (Literal[code.value], Field(description="Key of exactly this problem in the localization package")),  # type: ignore[valid-type]
    }
    for name, annotation in code.params.items():
        annotation = _PARAM_TYPES.get(name, annotation)
        fields[name] = (annotation, None) if is_optional(annotation) else (annotation, ...)
    model = create_model(schema_name(code), __config__=ConfigDict(extra="forbid"), **fields)
    model.__doc__ = f"{code.title} (HTTP {code.status}). Problem type {code.value}."
    return model


@lru_cache(maxsize=None)
def problem_models() -> Dict[ProblemType, type]:
    return {code: _problem_model(code) for cls in PROBLEM_ENUMS for code in cls}


def _example(code: ProblemType) -> Dict[str, Any]:
    params = {}
    for name, annotation in code.params.items():
        if is_optional(annotation):
            continue
        params[name] = _EXAMPLE_VALUES.get(name, f"<{name}>")
    try:
        detail = code.render(params)
    except (KeyError, IndexError, ValueError):
        detail = code.detail_template
    return {"type": f"https://example.org/problems/{code.value}", "title": code.title, "status": code.status,
            "detail": detail, "instance": f"{APIVersionRoutes.current().value}/...", "code": code.value, **params}


_EXAMPLE_VALUES: Dict[str, Any] = {
    "error_count": 1,
    "errors": [{"code": "string_too_short", "detail": "String should have at least 3 characters", "pointer": "#/username", "min_length": 3}],
    "error_id": "3f1c2a9b7d8e4f60a1b2c3d4e5f60718",
    "missing": ["post.view"],
    "required": {"all": ["post.view"], "any": [], "admin": False},
    "accepted_content_types": ["image/jpeg", "image/png"],
    "accepted_extensions": [".jpg", ".png"],
    "tag_ids": [7, 9],
    "extensions": [".png"],
    "content_types": ["image/png"],
    "assigned_users": [{"id": 3, "username": "operator1"}],
}
for _name in ("post_id", "role_id", "format_id", "offset", "sent_offset", "expected_size", "missing_bytes",
              "max_chunk_bytes", "max_bytes", "max_file_size_bytes", "ceiling_bytes", "max_requests",
              "window_seconds", "max_days", "attempts"):
    _EXAMPLE_VALUES.setdefault(_name, 1024)


def _absent_not_null(model_schema: Dict[str, Any]) -> Dict[str, Any]:
    """An optional member is left out of the body, never null: drop pydantic's null variant and default,
    so generators type it as optional (post_id?: number) and not as a present nullable value"""
    required = set(model_schema.get("required", ()))
    for name, prop in model_schema.get("properties", {}).items():
        if name in required:
            continue
        variants = [v for v in prop.get("anyOf", []) if v != {"type": "null"}]
        if "anyOf" in prop and len(variants) == 1:
            del prop["anyOf"]
            prop.update(variants[0])
        prop.pop("default", None)
    return model_schema


def _add_components(schema: Dict[str, Any]) -> None:
    schemas = schema.setdefault("components", {}).setdefault("schemas", {})
    for model in [RequiredPermissions, UserRef, FieldError]:
        model_schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
        schemas.update(model_schema.pop("$defs", {}))
        schemas[model.__name__] = _absent_not_null(model_schema) if model is FieldError else model_schema
    for code, model in problem_models().items():
        model_schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
        schemas.update(model_schema.pop("$defs", {}))
        model_schema["examples"] = [_example(code)]
        schemas[model.__name__] = _absent_not_null(model_schema)
    schemas["ProblemCode"] = {
        "type": "string", "enum": [code.value for cls in PROBLEM_ENUMS for code in cls],
        "description": "Every problem type of the API: the keys of the localization package",
    }
    schemas["FieldErrorCode"] = {
        "type": "string", "enum": [code.value for code in FieldErrorCode],
        "description": "Codes of the API for invalid values (items of validation_error); pydantic codes come on top",
    }
    schemas["Problem"] = {
        "description": "Any problem of the API (RFC 9457)",
        "oneOf": [{"$ref": f"#/components/schemas/{schema_name(code)}"} for code in problem_models()],
        "discriminator": {"propertyName": "code", "mapping": {
            code.value: f"#/components/schemas/{schema_name(code)}" for code in problem_models()}},
    }
    # FastAPI's own 422 schemas are replaced by validation_error
    schemas.pop("HTTPValidationError", None)
    schemas.pop("ValidationError", None)


def _responses_for(problems: Iterable[ProblemType]) -> Dict[str, Any]:
    by_status: Dict[int, List[ProblemType]] = {}
    for code in sorted(problems, key=lambda c: (c.status, c.value)):
        by_status.setdefault(code.status, []).append(code)
    responses = {}
    for status, codes in by_status.items():
        refs = [{"$ref": f"#/components/schemas/{schema_name(code)}"} for code in codes]
        # Examples live once per problem type in its component schema, not in every response
        media: Dict[str, Any] = {
            "schema": refs[0] if len(refs) == 1 else {
                "oneOf": refs,
                "discriminator": {"propertyName": "code", "mapping": {
                    code.value: f"#/components/schemas/{schema_name(code)}" for code in codes}},
            },
        }
        responses[str(status)] = {
            "description": "; ".join(f"`{code.value}`: {code.title}" for code in codes),
            "content": {PROBLEM_JSON: media},
        }
    return responses


def build_openapi(app: FastAPI) -> Dict[str, Any]:
    schema = get_openapi(title=app.title, version=app.version, description=app.description, routes=app.routes,
                         servers=app.servers, tags=app.openapi_tags)
    graph = CallGraph()
    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.include_in_schema:
            continue
        path_item = schema["paths"].get(route.path_format, {})
        for method in route.methods:
            operation = path_item.get(method.lower())
            if operation is None:
                continue
            responses = operation.setdefault("responses", {})
            responses.pop("422", None)  # FastAPI's list body; validation_error replaces it
            responses.update(_responses_for(route_problems(route, graph)))
    _add_components(schema)
    return schema


def install_openapi(app: FastAPI) -> None:
    """Replace app.openapi with the problem-aware schema (built once, then cached by FastAPI)"""

    def openapi() -> Dict[str, Any]:
        if app.openapi_schema is None:
            app.openapi_schema = build_openapi(app)
        return app.openapi_schema

    app.openapi = openapi
