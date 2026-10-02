"""The only way the API reports an error: RFC 9457 problem details, typed by enums/errors.py.

    raise ApiError(ErrorCode.POST_NOT_FOUND, post_id=post_id)
    -> 404 application/problem+json
       {"type": ".../problems/post_not_found", "title": "Post not found", "status": 404,
        "detail": "Post not found", "instance": "/api/v1/posts/7", "code": "post_not_found", "post_id": 7}

The status, the title and the allowed params come from the problem type; a param that the type does
not declare, or a missing required one, is a programming error (TypeError). Inside a request model
validator raise invalid(FieldErrorCode.X, **params) instead: pydantic turns it into an item of the
validation_error problem. Params are values for the translation of the code; never put exception text,
file paths, SQL or other server internals in them.
"""
from http import HTTPStatus
from typing import Any, Dict, Optional, Union, get_args, get_origin

from fastapi import HTTPException, Request
from fastapi.encoders import jsonable_encoder
from pydantic_core import PydanticCustomError

from app.backend.config import settings
from app.backend.enums.errors import ErrorCode, FieldCode, ProblemType
from app.backend.enums.routes import SystemRoutes

PROBLEM_JSON = "application/problem+json"
# RFC 9457 members and the localization key; params never use these names
RESERVED_MEMBERS = frozenset({"type", "title", "status", "detail", "instance", "code"})
# Members of an item of a validation_error besides its params
FIELD_MEMBERS = frozenset({"code", "detail", "pointer", "parameter", "location"})


def is_optional(annotation: Any) -> bool:
    return get_origin(annotation) is Union and type(None) in get_args(annotation)


def check_params(code: Union[ProblemType, FieldCode], params: Dict[str, Any], partial: bool = False) -> None:
    """Params must be exactly the ones the code declares (required ones present unless partial)"""
    reserved = FIELD_MEMBERS if isinstance(code, FieldCode) else RESERVED_MEMBERS
    unknown = set(params) - set(code.params)
    clash = reserved.intersection(params)
    missing = set() if partial else {name for name, annotation in code.params.items()
                                     if not is_optional(annotation) and name not in params}
    if unknown or clash or missing:
        raise TypeError(f"{code.value}: unknown params {sorted(unknown)}, reserved {sorted(clash)}, missing {sorted(missing)}")


class ApiError(HTTPException):
    """A problem of a declared type; the handler renders it as application/problem+json"""

    def __init__(self, code: ProblemType, headers: Optional[Dict[str, str]] = None, **params: Any):
        # An optional param without a value is left out of the body, never sent as null (see the OpenAPI schema)
        params = {k: v for k, v in params.items() if v is not None or not is_optional(code.params.get(k))}
        check_params(code, params)
        self.code = code
        self.params = params
        super().__init__(status_code=code.status, detail=code.render(params), headers=headers)


class CodedError(ValueError):
    """Domain failure raised by a service with its problem type; the route may add the params only it knows.
    A ValueError so callers that only know "rejected input" keep working."""

    def __init__(self, code: ProblemType, **params: Any):
        check_params(code, params, partial=True)
        self.code = code
        self.params = params
        super().__init__(f"{code.value}: {params}")

    def to_api_error(self, headers: Optional[Dict[str, str]] = None, **extra: Any) -> ApiError:
        return ApiError(self.code, headers=headers, **self.params, **extra)


def invalid(code: FieldCode, **params: Any) -> PydanticCustomError:
    """Item of a validation_error, for request model validators: raise invalid(FieldErrorCode.USERNAME_RESERVED)"""
    check_params(code, params)
    # The detail is rendered here so lists read like in ApiError; pydantic only fills {key} left in a template
    return PydanticCustomError(code.value, code.render(params), params)


def field_item(code: str, detail: str, loc: tuple, params: Dict[str, Any]) -> Dict[str, Any]:
    """{code, detail, pointer | parameter + location, **params}: pointer is a JSON Pointer into the body
    (RFC 6901, as in the example of RFC 9457 section 3); query, path, header and cookie values are named"""
    item: Dict[str, Any] = {"code": code, "detail": detail}
    where, *path = loc or ("body",)
    if where == "body":
        item["pointer"] = "#" + "".join("/" + str(p).replace("~", "~0").replace("/", "~1") for p in path)
    else:
        item["parameter"] = ".".join(str(p) for p in path)
        item["location"] = where
    for key, value in params.items():
        if key not in FIELD_MEMBERS:
            item[key] = value
    return item


def field_problem(code: FieldCode, loc: tuple, **params: Any) -> ApiError:
    """A validation_error with one item, for rules a route checks itself (loc like pydantic: ("body", "frequency_max"))"""
    check_params(code, params)
    return ApiError(ErrorCode.VALIDATION_ERROR, error_count=1, errors=[field_item(code.value, code.render(params), loc, params)])


def problem_type_uri(request: Request, code: str) -> str:
    """Absolute URI of the problem type; dereferencing it gives its documentation (RFC 9457 section 3.1.1)"""
    base = settings.app.problem_base_url.rstrip("/") or (str(request.base_url).rstrip("/") + SystemRoutes.PROBLEMS.value)
    return f"{base}/{code}"


def problem_body(request: Request, code: ProblemType, params: Dict[str, Any]) -> Dict[str, Any]:
    return jsonable_encoder({
        "type": problem_type_uri(request, code.value),
        "title": code.title,
        "status": code.status,
        "detail": code.render(params),
        "instance": request.url.path,
        "code": code.value,
        **params,
    })


def about_blank_body(request: Request, status_code: int) -> Dict[str, Any]:
    """RFC 9457 section 4.2.1: no more semantics than the status code; title is its phrase"""
    return {"type": "about:blank", "title": HTTPStatus(status_code).phrase, "status": status_code, "instance": request.url.path}
