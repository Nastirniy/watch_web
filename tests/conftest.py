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
from sqlmodel.ext.asyncio.session import AsyncSession
from app.backend.database import get_session, seed_roles_and_permissions
from app.backend.services.media.formats import seed_media_formats, invalidate_registry_cache
from app.backend.services.auth import AuthService


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
    async with AsyncClient(transport=transport, base_url="http://test") as async_client:
        yield async_client
    fastapi_app.dependency_overrides.clear()


def auth_headers(user) -> dict:
    """Bearer header for a user. No session id in the token, so no UserSession lookup happens."""
    return {"Authorization": f"Bearer {AuthService.create_access_token(user.id)}"}


@pytest.fixture
def headers():
    return auth_headers
