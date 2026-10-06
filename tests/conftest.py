"""Shared fixtures: in-memory sqlite database, FastAPI test client, seeded permissions.

The application lifespan (migrations + seed against MySQL) never runs under
httpx ASGITransport, so tests need no external database.
"""
import os

os.environ.setdefault("MYSETTINGS_APP__ENVIRONMENT", "development")

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool
from sqlmodel import SQLModel

import app.backend.models  # noqa: F401  registers every table on the metadata
from app.main import fastapi_app
from app.backend.api.versioning import PublishedVersion
from sqlmodel.ext.asyncio.session import AsyncSession
from app.backend.database import get_session, seed_roles_and_permissions
from app.backend.services.media.formats import seed_media_formats, invalidate_registry_cache
from app.backend.services.auth import AuthService
from tests.helpers import errors_are_problems


@pytest_asyncio.fixture
async def engine():
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session_maker(engine):
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


@pytest_asyncio.fixture
async def db(session_maker):
    """Session with roles, permissions and the default media formats seeded (admin role holds everything)"""
    async with session_maker() as session:
        await seed_roles_and_permissions(session)
        await seed_media_formats(session)
        invalidate_registry_cache()
        yield session
    invalidate_registry_cache()


@pytest_asyncio.fixture
async def client(session_maker, db):
    async def _override_get_session():
        async with session_maker() as session:
            yield session

    fastapi_app.dependency_overrides[get_session] = _override_get_session
    transport = ASGITransport(app=fastapi_app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test",
                           event_hooks={"response": [errors_are_problems]}) as async_client:
        yield async_client
    fastapi_app.dependency_overrides.clear()


@pytest.fixture(scope="session", autouse=True)
def problems_are_declared():
    """Every problem a test makes the app answer must be declared on its operation (responses=problems(...)).

    Records (method, path, code) at error_handlers.problem_response and checks them against the OpenAPI
    document of each version when the session ends. Framework problems belong to no operation and are not
    recorded."""
    from app.backend import error_handlers
    from app.backend.openapi_problems import FRAMEWORK_PROBLEMS, operation_problems

    recorded = set()
    original = error_handlers.problem_response

    def recording(request, code, params, headers=None):
        route = request.scope.get("route")
        if request.scope.get("app") is fastapi_app and route is not None and getattr(route, "include_in_schema", False) \
                and code not in FRAMEWORK_PROBLEMS:
            recorded.add((request.method, route.path_format, code.value))
        return original(request, code, params, headers)

    error_handlers.problem_response = recording
    yield recorded
    error_handlers.problem_response = original

    declared = {(method.upper(), path): {c for codes in operation_problems(operation).values() for c in codes}
                for version in PublishedVersion.published.values()
                for path, path_item in version.openapi(fastapi_app)["paths"].items()
                for method, operation in path_item.items()}
    undeclared = sorted(f"{method} {path}: {code}" for method, path, code in recorded
                        if code not in declared.get((method, path), set()))
    assert not undeclared, "answered but not declared in responses=problems(...):\n" + "\n".join(undeclared)


def auth_headers(user) -> dict:
    """Bearer header for a user. No session id in the token, so no UserSession lookup happens."""
    return {"Authorization": f"Bearer {AuthService.create_access_token(user.id)}"}


@pytest.fixture
def headers():
    return auth_headers
