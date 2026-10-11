"""Two API versions served in parallel, the way CLAUDE.md "Routes and API versions" describes them.

A throwaway second version publishes its own small module next to the real v1 on a test prefix: no shared
router, no re-publishing. Each version has its own OpenAPI document (operation ids and component names are
unique within a version), each handler builds URLs of its own version, /auth/route-access lists the routes of
the caller's version only.
"""
from enum import Enum
from types import SimpleNamespace

import pytest
import pytest_asyncio
from fastapi import APIRouter, Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel

from app.backend.api.v1 import V1
from app.backend.api.versioning import PublishedVersion, request_version
from app.backend.database import get_session
from app.backend.enums.general import Permissions
from app.backend.enums.routes import APIVersionRoutes, AuthRoutes, TagRoutes, UserRoutes
from app.backend.errors.handlers import register_error_handlers
from app.backend.auth.context import AuthContext
from app.backend.auth.require import require
from tests.helpers import make_admin


class ThrowawayVersions(str, Enum):
    V2 = "/api/test-v2"
    DUPLICATE = "/api/test-duplicate"


class TagResponse(BaseModel):
    """What api/v2/models/response/tags.py would define: the name of the v1 model, other fields"""
    id: int
    label: str


def _tags_module() -> SimpleNamespace:
    """What api/v2/tags.py would be: a module of its own, same path as in v1, a different handler"""
    router = APIRouter(prefix=TagRoutes.BASE.value, tags=[TagRoutes.tag()])

    @router.get(TagRoutes.LIST_ALL.value)
    async def list_tags(  # same function name as the v1 handler
            version: PublishedVersion = Depends(request_version),
            auth: AuthContext = Depends(require(all=[Permissions.TAG_VIEW])),
    ) -> dict:
        return {"version": version.version.value, "self": version.path(TagRoutes.LIST_ALL)}

    @router.post(TagRoutes.CREATE.value, response_model=TagResponse)
    async def create_tag(
            auth: AuthContext = Depends(require(all=[Permissions.TAG_VIEW, Permissions.TAG_CREATE], admin=True)),
    ) -> TagResponse:
        return TagResponse(id=1, label="new")

    return SimpleNamespace(router=router)


@pytest_asyncio.fixture
async def two_versions(session_maker):
    v2 = PublishedVersion(ThrowawayVersions.V2, _tags_module())
    app = FastAPI(generate_unique_id_function=lambda route: route.name)
    register_error_handlers(app)
    app.include_router(V1.router)
    app.include_router(v2.router)

    async def _session():
        async with session_maker() as session:
            yield session

    app.dependency_overrides[get_session] = _session
    async with AsyncClient(transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test") as client:
        yield app, v2, client
    PublishedVersion.published.pop(ThrowawayVersions.V2.value, None)


@pytest.mark.asyncio
async def test_both_versions_answer_with_their_own_handler(two_versions, db, headers):
    app, v2, client = two_versions
    admin = await make_admin(db)
    old = await client.get(V1.path(TagRoutes.LIST_ALL), headers=headers(admin))
    new = await client.get(v2.path(TagRoutes.LIST_ALL), headers=headers(admin))
    assert old.status_code == 200 and isinstance(old.json(), list)
    assert new.json() == {"version": ThrowawayVersions.V2.value, "self": v2.path(TagRoutes.LIST_ALL)}


def test_each_version_has_its_own_document(two_versions):
    """Both versions name their operation list_tags: an operation id is unique within its version's document"""
    app, v2, _ = two_versions
    for version in (V1, v2):
        document = version.openapi(app)
        ids = [op["operationId"] for item in document["paths"].values() for op in item.values()]
        assert len(ids) == len(set(ids)) and "list_tags" in ids
        assert all(version.owns(path) for path in document["paths"])


def test_a_model_named_like_a_v1_model_leaves_the_v1_types_alone(two_versions):
    """v2 defines its own TagResponse with other fields: each document keeps its own, under the plain name"""
    from app.main import fastapi_app
    app, v2, _ = two_versions
    v1_schemas = V1.openapi(app)["components"]["schemas"]
    v2_schemas = v2.openapi(app)["components"]["schemas"]
    assert v1_schemas["TagResponse"] == V1.openapi(fastapi_app)["components"]["schemas"]["TagResponse"]
    assert set(v2_schemas["TagResponse"]["properties"]) == {"id", "label"}
    assert [name for name in (*v1_schemas, *v2_schemas) if "__" in name] == []


def test_a_version_gives_paths_of_what_it_publishes_only(two_versions):
    _, v2, _ = two_versions
    assert v2.path(TagRoutes.BASE) == ThrowawayVersions.V2.value + TagRoutes.BASE.value
    with pytest.raises(LookupError):
        v2.path(UserRoutes.USER_BY_ID)  # v2 publishes no users module: clients keep calling v1 for users
    with pytest.raises(LookupError):
        v2.path(TagRoutes.TAG_BY_ID)


@pytest.mark.asyncio
async def test_route_access_lists_the_callers_version_only(two_versions, db, headers):
    app, v2, client = two_versions
    admin = await make_admin(db)
    routes = (await client.get(V1.path(AuthRoutes.ROUTE_ACCESS), headers=headers(admin))).json()["routes"]
    paths = {route["path"] for route in routes}
    assert V1.path(TagRoutes.LIST_ALL) in paths
    assert not any(path.startswith(ThrowawayVersions.V2.value) for path in paths)


def test_a_route_published_twice_in_one_version_is_rejected():
    module = _tags_module()
    with pytest.raises(ValueError, match="published twice"):
        PublishedVersion(ThrowawayVersions.DUPLICATE, module, module)
    assert ThrowawayVersions.DUPLICATE.value not in PublishedVersion.published


def test_a_prefix_is_published_once():
    with pytest.raises(ValueError, match="already published"):
        PublishedVersion(APIVersionRoutes.V1, _tags_module())
