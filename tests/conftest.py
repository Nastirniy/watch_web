"""Shared fixtures: in-memory sqlite database, FastAPI test client, seeded permissions.

The application lifespan (migrations + seed against MySQL) never runs under
httpx ASGITransport, so tests need no external database.
"""
import os
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("MYSETTINGS_APP__ENVIRONMENT", "development")

import httpx
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
from app.backend.database import get_session
from app.backend.bootstrap.permissions import seed_roles_and_permissions
from app.backend.services.media.formats import seed_media_formats, invalidate_registry_cache
from app.backend.auth import sessions as session_lifetimes
from app.backend.auth.service import AuthService
from app.backend.models.user import UserSession
from app.backend.config import settings
from app.backend.services.signer_client import use_signer_transport
from app.backend.signer.app import create_app as create_signer_app
from app.backend.signer.settings import SignerSettings
from tests import helpers as factories
from tests.helpers import errors_are_problems
from tests.pki_helpers import DownTransport, client_ca, write_ca_files


@pytest.fixture(autouse=True)
def _chosen_types_are_per_test():
    """Ids repeat between the tests' databases: the users whose type a test chose (tests/helpers.py) are per test"""
    factories._EXPLICIT_TYPES.clear()
    yield
    factories._EXPLICIT_TYPES.clear()


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


class _TestSession(AsyncSession):
    """Database session of the tests: on entering, it first stores the login sessions headers() issued since the
    last one, so a token minted in the test names a stored session by the time a request reads it (the request's
    database session comes from session_maker as well)"""

    async def __aenter__(self):
        issued = self.info.get("issued_sessions")
        if issued:
            self.add_all(issued)
            issued.clear()
            await self.commit()
        return self


@pytest.fixture
def issued_sessions() -> list:
    """UserSession rows headers() created and no database session has stored yet"""
    return []


@pytest_asyncio.fixture
async def session_maker(engine, issued_sessions):
    return async_sessionmaker(engine, class_=_TestSession, expire_on_commit=False,
                              info={"issued_sessions": issued_sessions})


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
    from app.backend.errors import handlers as error_handlers
    from app.backend.errors.openapi import FRAMEWORK_PROBLEMS, operation_problems

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


@pytest.fixture
def headers(issued_sessions):
    """headers(user): a login of the user. Every call opens a live session (development: bound to no certificate,
    no audit row) and returns the Bearer header of an access token bound to it, like POST /auth/login. The row is
    stored when the next database session opens (_TestSession), i.e. before the request that sends the header."""
    def auth_headers(user) -> dict:
        now = datetime.now(timezone.utc)
        row = UserSession(id=secrets.token_urlsafe(32), user_id=user.id, created_at=now, last_activity=now, expires_at=now)
        session_lifetimes.touch(row, now)
        issued_sessions.append(row)
        return {"Authorization": f"Bearer {AuthService.create_access_token(user.id, session_id=row.id)}"}
    return auth_headers


@dataclass
class PkiEnv:
    """A configured PKI: a throwaway client CA, an in-process signer, folders under tmp_path"""
    ca: tuple
    ca_cert_path: Path
    store_dir: Path
    crl_path: Path
    signer_settings: SignerSettings
    up_transport: httpx.AsyncBaseTransport

    def signer_down(self) -> None:
        use_signer_transport(DownTransport())

    def signer_up(self) -> None:
        use_signer_transport(self.up_transport)


@pytest.fixture
def pki(tmp_path, monkeypatch) -> PkiEnv:
    """settings.pki points at tmp_path; the signer client talks to an in-process signer (httpx.ASGITransport)"""
    ca = client_ca()
    ca_dir = tmp_path / "ca"
    ca_dir.mkdir()
    cert_path, key_path = write_ca_files(ca_dir, ca)
    store_dir, crl_dir = tmp_path / "issued", tmp_path / "crl"
    store_dir.mkdir()
    crl_dir.mkdir()
    signer_settings = SignerSettings(ca_cert_path=str(cert_path), ca_key_path=str(key_path))
    monkeypatch.setattr(settings.pki, "ca_cert_path", str(cert_path))
    monkeypatch.setattr(settings.pki, "signer_socket", str(tmp_path / "signer.sock"))
    monkeypatch.setattr(settings.pki, "store_dir", str(store_dir))
    monkeypatch.setattr(settings.pki, "crl_path", str(crl_dir / "nginx.crl"))
    monkeypatch.setattr(settings.pki, "extra_crl_paths", [])
    up = httpx.ASGITransport(app=create_signer_app(signer_settings))
    env = PkiEnv(ca=ca, ca_cert_path=cert_path, store_dir=store_dir, crl_path=crl_dir / "nginx.crl",
                 signer_settings=signer_settings, up_transport=up)
    env.signer_up()
    yield env
    use_signer_transport(None)
