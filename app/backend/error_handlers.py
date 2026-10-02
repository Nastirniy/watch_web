"""Every error of the API is an RFC 9457 problem (application/problem+json).

- ApiError: rendered from its problem type (enums/errors.py).
- Request validation (FastAPI's 422): the validation_error problem with one item per invalid value in
  "errors" ({code, detail, pointer | parameter + location, **params}); the submitted value is never echoed.
- Framework HTTP errors: unknown route, wrong method and unparsable body have problem types; any other
  status becomes "about:blank" with the status phrase as title (RFC 9457 section 4.2.1).
- Any unhandled exception: 500 internal_error with an error_id that is logged with the traceback.
- A 401 always carries WWW-Authenticate (RFC 9110 section 15.5.2).
"""
import uuid
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.backend.enums.errors import AuthErrorCode, ErrorCode, ProblemType
from app.backend.services.api_errors import (
    PROBLEM_JSON, ApiError, about_blank_body, field_item, problem_body,
)
from app.backend.utils.logging_config import get_api_logger

_logger = get_api_logger()

# Context keys pydantic adds that are server objects or repeat the input
DROPPED_CONTEXT = frozenset({"error", "input"})
_PLAIN = (str, int, float, bool, type(None))

# Framework errors raised with a plain string detail
_FRAMEWORK_PROBLEMS = {
    (404, "Not Found"): ErrorCode.ROUTE_NOT_FOUND,
    (405, "Method Not Allowed"): ErrorCode.METHOD_NOT_ALLOWED,
    (400, "There was an error parsing the body"): ErrorCode.BODY_PARSE_ERROR,
    (401, "Not authenticated"): AuthErrorCode.CREDENTIALS_REQUIRED,
    (403, "Not authenticated"): AuthErrorCode.CREDENTIALS_REQUIRED,
}


class ProblemResponse(JSONResponse):
    media_type = PROBLEM_JSON


def _plain(value: Any) -> bool:
    return isinstance(value, _PLAIN) or (isinstance(value, (list, tuple)) and all(isinstance(v, _PLAIN) for v in value))


def pydantic_item(error: Dict[str, Any]) -> Dict[str, Any]:
    """A pydantic error as an item of validation_error; codes from invalid() keep their params"""
    context = {k: (list(v) if isinstance(v, tuple) else v) for k, v in (error.get("ctx") or {}).items()
               if k not in DROPPED_CONTEXT and _plain(v)}
    return field_item(error["type"], error["msg"], tuple(error.get("loc", ())), context)


def _headers(status_code: int, headers: Optional[Dict[str, str]]) -> Dict[str, str]:
    headers = dict(headers or {})
    if status_code == 401 and not any(k.lower() == "www-authenticate" for k in headers):
        headers["WWW-Authenticate"] = "Bearer"
    return headers


def problem_response(request: Request, code: ProblemType, params: Dict[str, Any], headers: Optional[Dict[str, str]] = None) -> ProblemResponse:
    return ProblemResponse(status_code=code.status, content=problem_body(request, code, params), headers=_headers(code.status, headers))


async def validation_error_handler(request: Request, exc: RequestValidationError) -> ProblemResponse:
    errors = [pydantic_item(error) for error in exc.errors()]
    return problem_response(request, ErrorCode.VALIDATION_ERROR, {"error_count": len(errors), "errors": errors})


async def http_error_handler(request: Request, exc: StarletteHTTPException) -> Response:
    headers = getattr(exc, "headers", None)
    if exc.status_code in (204, 304):
        return Response(status_code=exc.status_code, headers=headers)
    if isinstance(exc, ApiError):
        return problem_response(request, exc.code, exc.params, headers)
    code = _FRAMEWORK_PROBLEMS.get((exc.status_code, exc.detail))
    if code is not None:
        return problem_response(request, code, {}, headers)
    # Only framework errors get here: routes raise ApiError (tests/test_error_contract.py)
    _logger.warning(f"HTTP {exc.status_code} without a problem type on {request.method} {request.url.path}: {exc.detail!r}")
    return ProblemResponse(status_code=exc.status_code, content=about_blank_body(request, exc.status_code),
                           headers=_headers(exc.status_code, headers))


async def unhandled_error_handler(request: Request, exc: Exception) -> ProblemResponse:
    error_id = uuid.uuid4().hex
    _logger.opt(exception=exc).error(f"Unhandled error {error_id} on {request.method} {request.url.path}: {exc!r}")
    return problem_response(request, ErrorCode.INTERNAL_ERROR, {"error_id": error_id})


def register_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(RequestValidationError, validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)
