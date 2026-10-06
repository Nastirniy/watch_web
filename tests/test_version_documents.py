"""Every API version has its own OpenAPI document and viewers, built by FastAPI's own functions.

GET /api/vN/openapi.json is get_openapi() over the routes of that version plus the unversioned ones (/health),
so a model of another version never shares or renames a component of this one. /api/vN/docs and /api/vN/redoc
are FastAPI's Swagger UI and ReDoc pages of that document. There is no document at the root.
"""
import pytest

from app.backend.api.v1 import V1
from app.backend.enums.routes import DocsRoutes, SystemRoutes
from app.main import fastapi_app


@pytest.mark.asyncio
async def test_version_document_is_served_at_the_version_prefix(client):
    response = await client.get(V1.path(DocsRoutes.OPENAPI_JSON))
    assert response.status_code == 200
    document = response.json()
    assert document == V1.openapi(fastapi_app)
    assert all(V1.owns(path) or path == SystemRoutes.HEALTH.value for path in document["paths"])
    assert SystemRoutes.HEALTH.value in document["paths"]


@pytest.mark.asyncio
async def test_viewers_load_the_version_document(client):
    for member in (DocsRoutes.SWAGGER_UI, DocsRoutes.REDOC):
        page = await client.get(V1.path(member))
        assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
        assert V1.path(DocsRoutes.OPENAPI_JSON) in page.text
    swagger = await client.get(V1.path(DocsRoutes.SWAGGER_UI))
    assert V1.path(DocsRoutes.SWAGGER_UI_OAUTH2_REDIRECT) in swagger.text
    assert (await client.get(V1.path(DocsRoutes.SWAGGER_UI_OAUTH2_REDIRECT))).status_code == 200


@pytest.mark.asyncio
async def test_no_document_at_the_root(client):
    for member in (DocsRoutes.OPENAPI_JSON, DocsRoutes.SWAGGER_UI, DocsRoutes.REDOC):
        response = await client.get(member.value)
        assert response.status_code == 404 and response.json()["code"] == "route_not_found"


def test_documents_are_not_operations():
    """The document and its viewers are not part of any document"""
    document = V1.openapi(fastapi_app)
    assert not any(path.startswith(V1.path(DocsRoutes.OPENAPI_JSON)) for path in document["paths"])
    assert not any(DocsRoutes.SWAGGER_UI.value in path or DocsRoutes.REDOC.value in path for path in document["paths"])
