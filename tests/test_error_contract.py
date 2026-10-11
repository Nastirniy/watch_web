"""Every error is an RFC 9457 problem typed by enums/errors.py; a code is the localization key of exactly one message.

Catalog checks follow RFC 9457 (title fixed per type, status per type, extension member names), static
checks keep the code on ApiError / access_denied / invalid(), runtime checks cover the bodies the
handlers produce, FastAPI's own errors included.
"""
import ast
import re
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.responses import StreamingResponse
from httpx import AsyncClient, ASGITransport
from pydantic import BaseModel, field_validator
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.backend.config import settings
from app.backend.enums.errors import (
    ALL_CODE_ENUMS, PROBLEM_ENUMS, AuthErrorCode, ErrorCode, FieldErrorCode, placeholders,
)
from app.backend.enums.routes import APIVersionRoutes, PostRoutes, SystemRoutes
from app.backend.api.v1 import V1
from app.backend.errors.handlers import UnhandledErrorMiddleware, register_error_handlers
from app.backend.request_id import RequestIdMiddleware
from app.backend.errors.coded import FIELD_MEMBERS, RESERVED_MEMBERS, invalid, is_optional
from app.backend.errors.problems import PROBLEM_JSON, ApiError
from app.main import fastapi_app
from tests.helpers import errors_are_problems, make_admin

APP_DIR = Path(__file__).resolve().parent.parent / "app" / "backend"
# The error machinery itself builds bodies directly
EXEMPT = {"errors/coded.py", "errors/problems.py", "errors/handlers.py"}
CODE_ENUMS = {cls.__name__: cls for cls in ALL_CODE_ENUMS}
# RFC 9457 section 3.2: extension member names
MEMBER_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")
# Request models of the API: shared ones and those of a single version
REQUEST_MODELS = re.compile(r"api/(shared|v\d+)/models/request/")


def _sources():
    for path in sorted(APP_DIR.rglob("*.py")):
        rel = path.relative_to(APP_DIR).as_posix()
        if rel.startswith("migrations/") or rel in EXEMPT:
            continue
        yield rel, ast.parse(path.read_text(encoding="utf-8"))


def _call_name(node: ast.Call):
    func = node.func
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None


def _code_member(node):
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id in CODE_ENUMS:
        return CODE_ENUMS[node.value.id][node.attr]
    return None


# ------------------------------------------------------------------ catalog

def test_codes_are_unique_snake_case_keys():
    seen = {}
    for cls in ALL_CODE_ENUMS:
        for member in cls:
            assert re.fullmatch(r"[a-z][a-z0-9_]*", member.value), member
            assert member.value not in seen, f"{member.value}: {seen.get(member.value)} and {cls.__name__}.{member.name}"
            seen[member.value] = f"{cls.__name__}.{member.name}"


def test_one_title_per_problem_type_and_one_text_per_field_code():
    """Two codes with the same text would be one message under two localization keys"""
    titles = {}
    for cls in PROBLEM_ENUMS:
        for member in cls:
            assert member.title not in titles, f"{member.value} repeats the title of {titles.get(member.title)}"
            titles[member.title] = member.value
            assert not placeholders(member.title), f"{member.value}: the title is fixed per problem type (RFC 9457 3.1.3)"
            assert 400 <= member.status <= 599, member
    details = {}
    for member in FieldErrorCode:
        assert member.detail_template not in details, f"{member.value} repeats {details.get(member.detail_template)}"
        details[member.detail_template] = member.value


def test_params_are_valid_extension_members_and_cover_the_templates():
    for cls in ALL_CODE_ENUMS:
        reserved = FIELD_MEMBERS if cls is FieldErrorCode else RESERVED_MEMBERS
        for member in cls:
            for name in member.params:
                assert MEMBER_NAME.fullmatch(name), f"{member.value}.{name}: RFC 9457 3.2 member name"
                assert name not in reserved, f"{member.value}.{name} is a reserved member"
            used = placeholders(member.detail_template)
            assert used <= set(member.params), f"{member.value}: template uses {used - set(member.params)}"
            optional_used = {n for n in used if is_optional(member.params[n])}
            assert not optional_used, f"{member.value}: an optional param cannot be in the text: {optional_used}"


# ------------------------------------------------------------------ static rules

def test_routes_never_build_error_bodies_by_hand():
    offenders = []
    for rel, tree in _sources():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _call_name(node) in ("HTTPException", "JSONResponse"):
                if _call_name(node) == "JSONResponse" and not any(kw.arg == "content" and "detail" in ast.unparse(kw.value) for kw in node.keywords):
                    continue
                offenders.append(f"{rel}:{node.lineno} {_call_name(node)}")
    assert not offenders, "raise ApiError(Code.X, **params) instead:\n" + "\n".join(offenders)


def test_request_validators_raise_coded_field_errors():
    """A plain ValueError in a validator answers pydantic's generic "value_error" code"""
    offenders = []
    scanned = []
    for rel, tree in _sources():
        if not (REQUEST_MODELS.match(rel) or rel == "utils/validators.py"):
            continue
        scanned.append(rel)
        for node in ast.walk(tree):
            if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) and _call_name(node.exc) == "ValueError":
                offenders.append(f"{rel}:{node.lineno}")
    assert any(REQUEST_MODELS.match(rel) for rel in scanned), "no request models found: REQUEST_MODELS is out of date"
    assert not offenders, "raise invalid(FieldErrorCode.X, **params) instead:\n" + "\n".join(offenders)


# Calls whose code is the first argument, and whether the route may add params later (partial)
_RAISERS = {"ApiError": False, "access_denied": False, "invalid": False, "field_problem": False,
            "CodedError": True, "UnsupportedMediaFormat": True, "ChunkTooLarge": True}


def test_every_raise_passes_exactly_the_declared_params():
    """The body must match the schema of its problem type (OpenAPI), and a missing placeholder would be a 500"""
    problems = []
    for rel, tree in _sources():
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and _call_name(node) in _RAISERS and node.args):
                continue
            member = _code_member(node.args[0])
            if member is None or any(kw.arg is None for kw in node.keywords):
                continue  # code chosen at runtime or **params: covered by the runtime check in ApiError
            name = _call_name(node)
            passed = {kw.arg for kw in node.keywords} - {"headers"}
            if name == "UnsupportedMediaFormat" and len(node.args) > 1:
                passed.add("media_type")
            unknown = passed - set(member.params)
            missing = set() if _RAISERS[name] else {
                p for p, annotation in member.params.items() if not is_optional(annotation) and p not in passed}
            if unknown or missing:
                problems.append(f"{rel}:{node.lineno} {member.value}: unknown {sorted(unknown)}, missing {sorted(missing)}")
    assert not problems, "\n".join(problems)


def test_api_error_rejects_undeclared_or_missing_params():
    with pytest.raises(TypeError):
        ApiError(ErrorCode.POST_NOT_FOUND, name="x")
    with pytest.raises(TypeError):
        ApiError(ErrorCode.TAG_NAME_TAKEN)
    with pytest.raises(TypeError):
        invalid(FieldErrorCode.STRING_TOO_LONG)


# ------------------------------------------------------------------ runtime bodies

def _probe_app() -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)

    class Body(BaseModel):
        name: str

        @field_validator("name")
        @classmethod
        def reserved(cls, v):
            if v == "admin":
                raise invalid(FieldErrorCode.USERNAME_RESERVED)
            if v == "long":
                raise invalid(FieldErrorCode.STRING_TOO_LONG, max_length=3)
            return v

    @app.post("/items")
    async def create(body: Body, page: int = 1):
        return {"ok": True}

    @app.get("/boom")
    async def boom():
        raise RuntimeError("secret path /srv/data/x.db")

    @app.get("/broken-stream")
    async def broken_stream():
        async def chunks():
            yield b"started"
            raise RuntimeError("broke after the response started")
        return StreamingResponse(chunks())

    @app.get("/coded")
    async def coded():
        raise ApiError(ErrorCode.TAG_NAME_TAKEN, name="radar")

    @app.get("/teapot")
    async def teapot():
        raise StarletteHTTPException(status_code=418)

    @app.get("/auth")
    async def auth():
        raise ApiError(AuthErrorCode.INVALID_TOKEN)

    return app


@pytest_asyncio.fixture
async def probe():
    transport = ASGITransport(app=_probe_app(), raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://probe") as client:
        yield client


def _problem(response, status: int) -> dict:
    assert response.status_code == status, response.text
    assert response.headers["content-type"] == PROBLEM_JSON
    body = response.json()
    assert body["status"] == status
    return body


@pytest.mark.asyncio
async def test_api_error_body_is_a_problem(probe):
    body = _problem(await probe.get("/coded"), 400)
    assert body == {
        "type": "http://probe/problems/tag_name_taken",
        "title": "Tag name already exists",
        "status": 400,
        "detail": "Tag with name 'radar' already exists",
        "instance": "/coded",
        "code": "tag_name_taken",
        "name": "radar",
    }


@pytest.mark.asyncio
async def test_problem_base_url_setting(probe, monkeypatch):
    monkeypatch.setattr(settings.app, "problem_base_url", "https://monitoring.example/problems/")
    body = _problem(await probe.get("/coded"), 400)
    assert body["type"] == "https://monitoring.example/problems/tag_name_taken"


@pytest.mark.asyncio
async def test_validation_errors_are_one_problem_without_the_input(probe):
    body = _problem(await probe.post("/items?page=x", json={"name": "admin"}), 422)
    assert body["code"] == "validation_error" and body["title"] == "Request validation failed"
    assert body["error_count"] == 2 and body["detail"] == "The request has 2 invalid value(s)"
    by_code = {e["code"]: e for e in body["errors"]}
    assert by_code["username_reserved"] == {"code": "username_reserved", "detail": "Username is reserved", "pointer": "#/name"}
    assert by_code["int_parsing"]["parameter"] == "page" and by_code["int_parsing"]["location"] == "query"
    assert all("input" not in e and "ctx" not in e and "loc" not in e for e in body["errors"])


@pytest.mark.asyncio
async def test_field_error_params_are_members_of_the_item(probe):
    body = _problem(await probe.post("/items", json={"name": "long"}), 422)
    assert body["errors"] == [{"code": "string_too_long", "detail": "String should have at most 3 characters",
                               "pointer": "#/name", "max_length": 3}]


@pytest.mark.asyncio
async def test_missing_field_and_bad_json(probe):
    missing = _problem(await probe.post("/items", json={}), 422)["errors"][0]
    assert missing["code"] == "missing" and missing["pointer"] == "#/name"
    bad_json = _problem(await probe.post("/items", content=b"{not json", headers={"content-type": "application/json"}), 422)
    assert bad_json["errors"][0]["code"] == "json_invalid"


@pytest.mark.asyncio
async def test_framework_errors_get_problem_types(probe):
    not_found = _problem(await probe.get("/nowhere"), 404)
    assert not_found["code"] == "route_not_found" and not_found["instance"] == "/nowhere"
    wrong_method = await probe.delete("/items")
    assert _problem(wrong_method, 405)["code"] == "method_not_allowed"
    assert wrong_method.headers["allow"] == "POST"


@pytest.mark.asyncio
async def test_other_statuses_are_about_blank(probe):
    body = _problem(await probe.get("/teapot"), 418)
    assert body == {"type": "about:blank", "title": "I'm a Teapot", "status": 418, "instance": "/teapot"}


@pytest.mark.asyncio
async def test_unauthorized_carries_www_authenticate(probe):
    response = await probe.get("/auth")
    assert _problem(response, 401)["code"] == "invalid_token"
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.asyncio
async def test_unhandled_exception_hides_its_text(probe):
    body = _problem(await probe.get("/boom"), 500)
    assert body["code"] == "internal_error"
    assert re.fullmatch(r"[0-9a-f]{32}", body["error_id"])
    assert body["error_id"] in body["detail"] and "secret" not in str(body)


@pytest.mark.asyncio
async def test_an_answered_error_does_not_reach_the_server():
    """The 500 is the answer: Starlette's handler for Exception re-raised it after answering, and the server
    logged the error a second time, without its error_id (tests/test_server_logging.py)"""
    transport = ASGITransport(app=_probe_app(), raise_app_exceptions=True)
    async with AsyncClient(transport=transport, base_url="http://probe") as client:
        assert _problem(await client.get("/boom"), 500)["code"] == "internal_error"


@pytest.mark.asyncio
async def test_a_failure_after_the_response_started_goes_to_the_server():
    """Nothing can be answered once the status line is sent: the server logs the error and drops the connection"""
    transport = ASGITransport(app=_probe_app(), raise_app_exceptions=True)
    async with AsyncClient(transport=transport, base_url="http://probe") as client:
        with pytest.raises(RuntimeError, match="after the response started"):
            await client.get("/broken-stream")


# ------------------------------------------------------------------ the real app

@pytest.mark.asyncio
async def test_the_suite_rejects_an_api_error_without_a_problem_body():
    """The `client` of every test checks each answer: an error of the API is always a problem (API.md). What the
    infrastructure answers on its own (nginx, a proxy) is the client's to handle by status, never the API's"""
    request = httpx.Request("GET", "http://test/anything")
    with pytest.raises(AssertionError, match="without application/problem\\+json"):
        await errors_are_problems(httpx.Response(404, headers={"content-type": "text/html"}, request=request))
    await errors_are_problems(httpx.Response(404, headers={"content-type": PROBLEM_JSON}, request=request))
    await errors_are_problems(httpx.Response(200, headers={"content-type": "text/html"}, request=request))

def test_unhandled_errors_are_answered_by_the_outermost_middleware_but_the_request_id():
    """A middleware added after register_error_handlers() would sit outside it: its errors would get Starlette's
    plain-text 500 without an error_id. Only RequestIdMiddleware is outside: the 500 carries the request id too"""
    assert [m.cls for m in fastapi_app.user_middleware[:2]] == [RequestIdMiddleware, UnhandledErrorMiddleware]


@pytest.mark.asyncio
async def test_real_app_answers_problems(client, db, headers):
    admin = await make_admin(db)
    missing_post = await client.get(V1.path(PostRoutes.POST_BY_ID, post_id=999999), headers=headers(admin))
    body = _problem(missing_post, 404)
    assert body["code"] == "post_not_found" and body["type"].endswith(f"{SystemRoutes.PROBLEMS.value}/post_not_found")

    no_token = await client.get(V1.path(PostRoutes.ROOT))
    assert _problem(no_token, 401)["code"] == AuthErrorCode.CREDENTIALS_REQUIRED.value
    assert no_token.headers["www-authenticate"] == "Bearer"

    unknown = await client.get(APIVersionRoutes.V1.value + "/does-not-exist", headers=headers(admin))
    assert _problem(unknown, 404)["code"] == "route_not_found"


@pytest.mark.asyncio
async def test_problem_type_uri_dereferences_to_its_documentation(client, db, headers):
    admin = await make_admin(db)
    missing_post = await client.get(V1.path(PostRoutes.POST_BY_ID, post_id=999999), headers=headers(admin))
    page = await client.get(missing_post.json()["type"])
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    assert "post_not_found" in page.text and "Post not found" in page.text
    assert "GET /api/v1/posts/{post_id}" in page.text  # the routes that answer it

    index = await client.get(SystemRoutes.PROBLEMS.value)
    assert index.status_code == 200 and all(code.value in index.text for cls in PROBLEM_ENUMS for code in cls)

    unknown = await client.get(SystemRoutes.PROBLEM_BY_CODE.value.format(code="nope"))
    assert _problem(unknown, 404)["code"] == "problem_type_not_found"
