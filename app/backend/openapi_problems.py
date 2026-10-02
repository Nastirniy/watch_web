"""Problems (RFC 9457) in OpenAPI, the FastAPI way.

- problem_models(): one pydantic model per problem type of enums/errors.py: const code / title / status and
  typed params; an optional param is absent from the body, never null, and its schema says so.
- problems(*codes): the `responses=` of a route, a router or the app. A route declares the codes it answers
  on its decorator; generic ones are declared once: internal_error on the app, the 401 set and
  validation_error on each version router (FastAPI merges them per status).
- install_openapi(): FastAPI's "Extending OpenAPI" hook, only for what FastAPI cannot express itself: error
  content under application/problem+json, and the schemas of the problems no operation can declare
  (framework errors: unknown route, wrong method, unparsable body, unknown problem page).

tests/test_openapi.py checks the document; tests/conftest.py checks that every problem the test suite makes
the app answer is declared on its operation.
"""
from functools import lru_cache
from typing import Annotated, Any, Dict, List, Literal, Optional, Set, Union, get_args, get_origin

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict, Field, create_model

from app.backend.enums.errors import (
    PROBLEM_ENUMS, AccessErrorCode, AuthErrorCode, ErrorCode, FieldErrorCode, ProblemType,
)
from app.backend.enums.routes import APIVersionRoutes
from app.backend.services.api_errors import PROBLEM_JSON, is_optional

# Answered by the framework outside any operation; their schemas are added to the components by the hook
FRAMEWORK_PROBLEMS = (ErrorCode.ROUTE_NOT_FOUND, ErrorCode.METHOD_NOT_ALLOWED, ErrorCode.BODY_PARSE_ERROR,
                      ErrorCode.PROBLEM_TYPE_NOT_FOUND)


# ============================================================ models

def _camel(code: str) -> str:
    return "".join(part.capitalize() for part in code.split("_"))


def schema_name(code: ProblemType) -> str:
    return f"{_camel(code.value)}Problem"


def _absent_not_null(schema: Dict[str, Any]) -> None:
    """An optional member is left out of the body, never null: no null variant and no default in its schema,
    so generators type it as optional (post_id?: number) and not as a present nullable value"""
    required = set(schema.get("required", ()))
    for name, prop in schema.get("properties", {}).items():
        if name in required:
            continue
        variants = [v for v in prop.get("anyOf", []) if v != {"type": "null"}]
        if "anyOf" in prop and len(variants) == 1:
            del prop["anyOf"]
            prop.update(variants[0])
        prop.pop("default", None)


class FieldError(BaseModel):
    """One invalid value of a validation_error. Codes of the API are listed in FieldErrorCode; built-in
    checks keep pydantic's codes (missing, string_too_short, int_parsing, json_invalid, ...). Other
    members are the params of the code (min_length, max_length, accepted, ...)."""
    model_config = ConfigDict(extra="allow", json_schema_extra=lambda schema, cls: _absent_not_null(schema))

    # FieldErrorCode lists the codes of the API (a component of its own in OpenAPI); pydantic's come on top
    code: Union[FieldErrorCode, str] = Field(description="Key of exactly this message in the localization package")
    detail: str = Field(description="English text of the message")
    pointer: Optional[str] = Field(default=None, description='JSON Pointer (RFC 6901) into the body, e.g. "#/username"')
    parameter: Optional[str] = Field(default=None, description="Name of the query, path, header or cookie parameter")
    location: Optional[Literal["query", "path", "header", "cookie"]] = Field(default=None, description="Where parameter is")


_PARAM_TYPES = {"errors": List[FieldError]}

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
            "detail": detail, "instance": f"{APIVersionRoutes.V1.value}/...", "code": code.value, **params}


def _problem_schema_extra(code: ProblemType):
    def extra(schema: Dict[str, Any], cls: type) -> None:
        _absent_not_null(schema)
        schema["examples"] = [_example(code)]
    return extra


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
    model = create_model(schema_name(code), __config__=ConfigDict(extra="forbid", json_schema_extra=_problem_schema_extra(code)),
                         **fields)
    model.__doc__ = f"{code.title} (HTTP {code.status}). Problem type {code.value}."
    return model


@lru_cache(maxsize=None)
def problem_models() -> Dict[ProblemType, type]:
    return {code: _problem_model(code) for cls in PROBLEM_ENUMS for code in cls}


# ============================================================ declaring the problems of a route

# 401 of every route behind require(): the version routers declare it (login and refresh declare their own)
AUTHENTICATED = (AuthErrorCode.CREDENTIALS_REQUIRED, AuthErrorCode.INVALID_TOKEN, AuthErrorCode.SESSION_EXPIRED,
                 AuthErrorCode.ACCOUNT_NOT_FOUND, AuthErrorCode.ACCOUNT_DEACTIVATED, AuthErrorCode.ACCOUNT_DELETED)
# 403 of require(all=[...]) and of require(all=[...], admin=True)
GUARDED = (AccessErrorCode.PERMISSION_DENIED,)
ADMIN = (AccessErrorCode.PERMISSION_DENIED, AccessErrorCode.ADMIN_REQUIRED)

# FastAPI merges responses per status by replacement (app, version router, route): a route that declares
# its own 500 or 422 keeps the generic member of that status
_KEPT_IN_STATUS = {500: ErrorCode.INTERNAL_ERROR, 422: ErrorCode.VALIDATION_ERROR}


def problems(*codes: ProblemType) -> Dict[int, Dict[str, Any]]:
    """`responses=` of a route, app or router: the problem types it answers, grouped by status.

        @router.put(PostRoutes.POST_BY_ID.value, response_model=PostResponse,
                    responses=problems(ErrorCode.POST_NOT_FOUND, ErrorCode.POST_CODE_TAKEN, *ADMIN))

    One code per status is its model, several are a union discriminated by "code"; FastAPI registers the
    component schemas itself. Combine with success helpers: responses={**file_response(...), **problems(...)}."""
    by_status: Dict[int, set] = {}
    for code in codes:
        by_status.setdefault(code.status, set()).add(code)
    for status, kept in _KEPT_IN_STATUS.items():
        if status in by_status:
            by_status[status].add(kept)
    responses: Dict[int, Dict[str, Any]] = {}
    for status in sorted(by_status):
        group = sorted(by_status[status], key=lambda c: c.value)
        models = tuple(problem_models()[code] for code in group)
        model = models[0] if len(models) == 1 else Annotated[Union[models], Field(discriminator="code")]
        responses[status] = {"description": "; ".join(f"`{code.value}`: {code.title}" for code in group), "model": model}
    return responses


def declared_problems(responses: Dict[Any, Dict[str, Any]]) -> Set[ProblemType]:
    """The problem types in a `responses=` dict built by problems() (route.responses holds the merged one)"""
    by_model = {model: code for code, model in problem_models().items()}
    found: Set[ProblemType] = set()
    for response in responses.values():
        model = response.get("model")
        if model is None:
            continue
        for candidate in get_args(get_args(model)[0]) if get_origin(model) is Annotated else (model,):
            if candidate in by_model:
                found.add(by_model[candidate])
    return found


def operation_problems(operation: Dict[str, Any]) -> Dict[str, List[str]]:
    """status -> problem codes of one operation of the OpenAPI document"""
    code_of = {schema_name(code): code.value for code in problem_models()}
    found: Dict[str, List[str]] = {}
    for status, response in operation.get("responses", {}).items():
        schema = response.get("content", {}).get(PROBLEM_JSON, {}).get("schema", {})
        refs = [item["$ref"] for item in schema.get("oneOf", [schema]) if "$ref" in item]
        codes = sorted(code_of[ref.rsplit("/", 1)[1]] for ref in refs if ref.rsplit("/", 1)[1] in code_of)
        if codes:
            found[str(status)] = codes
    return found


# ============================================================ the OpenAPI document

def _add_component(schemas: Dict[str, Any], model: type) -> None:
    model_schema = model.model_json_schema(ref_template="#/components/schemas/{model}", mode="serialization")
    for name, definition in model_schema.pop("$defs", {}).items():
        schemas.setdefault(name, definition)
    schemas.setdefault(model.__name__, model_schema)


def _finish(schema: Dict[str, Any]) -> None:
    for path_item in schema.get("paths", {}).values():
        for operation in path_item.values():
            for status, response in operation.get("responses", {}).items():
                if str(status)[:1] not in ("4", "5") or "content" not in response:
                    continue
                # FastAPI puts additional responses under the route's media type; errors are problem+json
                media = next(iter(response["content"].values()))
                media.get("schema", {}).pop("title", None)  # FastAPI names each union after the route
                response["content"] = {PROBLEM_JSON: media}
    schemas = schema.setdefault("components", {}).setdefault("schemas", {})
    for code in FRAMEWORK_PROBLEMS:
        _add_component(schemas, problem_models()[code])


def install_openapi(app: FastAPI) -> None:
    """FastAPI's "Extending OpenAPI": the document FastAPI builds, finished once and cached"""
    build = app.openapi

    def openapi() -> Dict[str, Any]:
        if app.openapi_schema is None:
            _finish(build())
        return app.openapi_schema

    app.openapi = openapi
