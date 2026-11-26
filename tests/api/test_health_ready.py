"""GET /health/ready: whether this process may take requests (the compose healthcheck and deploy/release.sh wait on
it), and the schema part of /health (RELEASE.md, "Verify")."""
import json

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import OperationalError

from app.backend import build_info as build_info_module
from app.backend.api.system import health_checks
from app.backend.bootstrap import schema_range
from app.backend.config import settings
from app.backend.database import get_session
from app.backend.enums.routes import SystemRoutes
from app.backend.utils.alembic_history import code_head
from app.main import fastapi_app

READY = SystemRoutes.HEALTH_READY.value


@pytest.fixture(autouse=True)
def fresh(monkeypatch, tmp_path):
    path = tmp_path / "build_info.json"
    path.write_text(json.dumps({"version": "2026.11.24-2a8c1e7", "revision": "f" * 40, "created": None}))
    monkeypatch.setattr(build_info_module, "BUILD_INFO_PATH", path)
    build_info_module.build_info.cache_clear()
    health_checks.reset_readiness_cache()
    health_checks.ready_rate_limiter.requests.clear()
    health_checks.health_rate_limiter.requests.clear()
    yield
    build_info_module.build_info.cache_clear()
    health_checks.reset_readiness_cache()


async def set_state(engine, revision, min_code_head=None):
    async with engine.begin() as conn:
        await conn.execute(sa.text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)"))
        await conn.execute(sa.text("DELETE FROM alembic_version"))
        await conn.execute(sa.text("INSERT INTO alembic_version VALUES (:v)"), {"v": revision})
        await conn.run_sync(lambda sync: schema_range.write_min_code_head(sync, min_code_head))


async def test_ready_with_version_and_schema_revision(client, engine):
    await set_state(engine, code_head())
    response = await client.get(READY)
    assert response.status_code == 200, response.text
    assert response.json() == {"ready": True, "version": "2026.11.24-2a8c1e7", "revision": "f" * 40,
                               "schema_revision": code_head()}


async def test_ready_on_a_newer_schema_that_admits_this_release(client, engine):
    await set_state(engine, "999_future")
    response = await client.get(READY)
    assert response.status_code == 200
    assert response.json()["schema_revision"] == "999_future"


async def test_schema_out_of_range(client, engine):
    await set_state(engine, "999_future", "999_future")
    response = await client.get(READY)
    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "service_not_ready" and body["reason_code"] == "schema_out_of_range"
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.headers["retry-after"] == "10"


async def test_database_unreachable(client, engine):
    class Broken:
        async def connection(self):
            raise OperationalError("SELECT 1", {}, ConnectionRefusedError("refused"))

    async def broken_session():
        yield Broken()

    fastapi_app.dependency_overrides[get_session] = broken_session
    response = await client.get(READY)
    assert response.status_code == 503
    assert response.json()["reason_code"] == "database_unreachable"


async def test_the_answer_is_cached_for_ten_seconds(client, engine, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(health_checks.time, "monotonic", lambda: clock[0])
    await set_state(engine, code_head())
    assert (await client.get(READY)).status_code == 200
    await set_state(engine, "999_future", "999_future")
    assert (await client.get(READY)).status_code == 200, "still the cached answer"
    clock[0] += 10.5
    assert (await client.get(READY)).status_code == 503


async def test_the_event_hub_does_not_matter(client, engine):
    await set_state(engine, code_head())
    fastapi_app.state.event_hub = None
    assert (await client.get(READY)).status_code == 200


async def test_host_check(client, engine, monkeypatch):
    await set_state(engine, code_head())
    monkeypatch.setattr(settings.health, "enable_host_check", True)
    monkeypatch.setattr(settings.health, "allowed_hosts", ["10.9.9.9"])
    response = await client.get(READY)
    assert response.status_code == 403 and response.json()["code"] == "health_host_not_allowed"


async def test_its_own_rate_bucket(client, engine, monkeypatch):
    await set_state(engine, code_head())
    monkeypatch.setattr(settings.health, "enable_rate_limit", True)
    monkeypatch.setattr(settings.health, "rate_limit_requests", 1)
    monkeypatch.setattr(settings.health, "ready_rate_limit_requests", 3)
    assert (await client.get(SystemRoutes.HEALTH.value)).status_code == 200
    assert (await client.get(SystemRoutes.HEALTH.value)).status_code == 429
    for _ in range(3):
        assert (await client.get(READY)).status_code == 200, "/health's limit does not touch readiness"
    limited = await client.get(READY)
    assert limited.status_code == 429
    assert limited.json()["max_requests"] == 3


def test_ready_limits_are_settings():
    assert settings.health.ready_rate_limit_requests == 60
    assert settings.health.ready_rate_limit_window_seconds == 60


async def test_health_has_a_schema_part(client, engine, monkeypatch):
    monkeypatch.setattr(settings.health, "enable_rate_limit", False)
    await set_state(engine, code_head())
    body = (await client.get(SystemRoutes.HEALTH.value)).json()
    assert body["schema"] == {"revision": code_head(), "code_head": code_head(), "min_code_head": None,
                              "compatible": True}
    await set_state(engine, "999_future", "999_future")
    body = (await client.get(SystemRoutes.HEALTH.value)).json()
    assert body["schema"]["compatible"] is False and body["schema"]["min_code_head"] == "999_future"
    assert body["status"] != "healthy"
