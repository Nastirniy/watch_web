"""The request pool exhausted (sqlalchemy.exc.TimeoutError while waiting for a connection): a fast 503
database_busy with Retry-After, never a 500 after a long wait (plan Task 14 test 5, 2026-11-23: under a refetch storm
the API answered 500 internal_error after 30 s)."""
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.exc import TimeoutError as PoolTimeout

from app.backend.config import settings
from app.backend.database import get_session
from app.backend.enums.errors import ErrorCode
from app.backend.enums.routes import TagRoutes
from app.backend.api.v1 import V1
from app.backend.errors.handlers import DATABASE_BUSY_RETRY_AFTER_SECONDS, register_error_handlers
from app.main import fastapi_app


def _app_failing_with(exc: Exception) -> FastAPI:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/needs-the-database")
    async def needs_the_database():
        raise exc

    return app


async def _get(app: FastAPI, path: str) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(path)


async def test_a_pool_timeout_is_503_database_busy_with_retry_after():
    response = await _get(_app_failing_with(PoolTimeout("QueuePool limit of size 10 overflow 10 reached")),
                          "/needs-the-database")
    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.headers["retry-after"] == str(DATABASE_BUSY_RETRY_AFTER_SECONDS)
    body = response.json()
    assert body["code"] == ErrorCode.DATABASE_BUSY.value and "error_id" not in body
    assert "QueuePool" not in response.text, "no exception text in the answer"


async def test_any_other_error_stays_500_internal_error():
    response = await _get(_app_failing_with(RuntimeError("boom")), "/needs-the-database")
    assert response.status_code == 500 and response.json()["code"] == ErrorCode.INTERNAL_ERROR.value


async def test_a_real_route_answers_database_busy_when_no_connection_is_free():
    async def no_connection():
        raise PoolTimeout("QueuePool limit reached")
        yield  # pragma: no cover

    fastapi_app.dependency_overrides[get_session] = no_connection
    try:
        response = await _get(fastapi_app, V1.path(TagRoutes.LIST_ALL))
    finally:
        fastapi_app.dependency_overrides.pop(get_session, None)
    assert response.status_code == 503 and response.json()["code"] == "database_busy"


def test_every_operation_declares_database_busy_also_next_to_a_503_of_its_own():
    document = V1.openapi(fastapi_app)
    for path, item in document["paths"].items():
        for method, operation in item.items():
            text = str(operation["responses"].get("503", ""))
            assert "DatabaseBusy" in text or "database_busy" in text, f"{method.upper()} {path}"


def test_the_request_pool_waits_only_pool_timeout_seconds():
    assert 1 <= settings.database.pool_timeout_seconds <= 10
