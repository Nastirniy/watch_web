"""A published API version: the routers it publishes and the full paths of its routes.

A version publishes exactly the modules of its own folder (app/backend/api/vN/). Versions never import
each other and never share routers, so v1 and v2 run side by side on their own prefixes; a client moves
to a new version resource by resource. See CLAUDE.md, "Routes and API versions".

    V1 = PublishedVersion(APIVersionRoutes.V1, auth, users, ...)      # app/backend/api/v1/__init__.py
    V1.path(PostRoutes.POST_BY_ID, post_id=7)                          # /api/v1/posts/7
    version: PublishedVersion = Depends(request_version)               # in a route that returns URLs

Each version serves its own OpenAPI document and viewers (/api/v1/openapi.json, /docs, /redoc), the way
FastAPI.setup() serves them for an app: a component name is unique within one version only.
"""
from enum import Enum
from types import ModuleType
from typing import Any, ClassVar, Dict, Set
from weakref import WeakKeyDictionary

from fastapi import APIRouter, FastAPI, Request
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html, get_swagger_ui_oauth2_redirect_html
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.routing import APIRoute

from app.backend.enums.errors import ErrorCode
from app.backend.enums.routes import APIVersionRoutes, DocsRoutes
from app.backend.openapi_problems import AUTHENTICATED, openapi_document, problems


class PublishedVersion:
    """The router of one API version, built from the `router` of each module it publishes"""

    published: ClassVar[Dict[str, "PublishedVersion"]] = {}

    def __init__(self, version: APIVersionRoutes, *modules: ModuleType):
        if version.value in self.published:
            raise ValueError(f"{version.value} is already published")
        self.version = version
        # Every route of a version is behind require() (login and refresh declare their own 401) and may
        # answer validation_error; 500 internal_error comes from the app
        self.router = APIRouter(prefix=version.value, responses=problems(*AUTHENTICATED, ErrorCode.VALIDATION_ERROR))
        for module in modules:
            self.router.include_router(module.router)
        self.templates: Set[str] = self._check_routes()
        self._documents: WeakKeyDictionary = WeakKeyDictionary()
        self._serve_documentation()
        self.published[version.value] = self

    def _check_routes(self) -> Set[str]:
        """FastAPI only warns about a repeated operation and the first route silently wins"""
        methods: Dict[tuple, str] = {}
        names: Dict[str, str] = {}
        for route in self.router.routes:
            if not isinstance(route, APIRoute):
                continue
            for method in route.methods:
                key = (method, route.path_format)
                if key in methods:
                    raise ValueError(f"{method} {route.path_format} is published twice in {self.version.value}: "
                                     f"{methods[key]} and {route.name}")
                methods[key] = route.name
            if names.setdefault(route.name, route.path_format) != route.path_format:
                raise ValueError(f"operation {route.name} is published twice in {self.version.value}")
        return {path for _, path in methods}

    def _serve_documentation(self) -> None:
        """What FastAPI.setup() does for an app, for this version: its document, Swagger UI and ReDoc"""
        openapi_url = self.version.value + DocsRoutes.OPENAPI_JSON.value
        oauth2_redirect_url = self.version.value + DocsRoutes.SWAGGER_UI_OAUTH2_REDIRECT.value
        name = self.version.name.lower()

        async def openapi(req: Request) -> JSONResponse:
            return JSONResponse(self.openapi(req.app))

        async def swagger_ui_html(req: Request) -> HTMLResponse:
            root_path = req.scope.get("root_path", "").rstrip("/")
            return get_swagger_ui_html(
                openapi_url=root_path + openapi_url,
                title=f"{req.app.title} {name} - Swagger UI",
                oauth2_redirect_url=root_path + oauth2_redirect_url,
                init_oauth=req.app.swagger_ui_init_oauth,
                swagger_ui_parameters=req.app.swagger_ui_parameters,
            )

        async def swagger_ui_redirect(req: Request) -> HTMLResponse:
            return get_swagger_ui_oauth2_redirect_html()

        async def redoc_html(req: Request) -> HTMLResponse:
            root_path = req.scope.get("root_path", "").rstrip("/")
            return get_redoc_html(openapi_url=root_path + openapi_url, title=f"{req.app.title} {name} - ReDoc")

        for member, endpoint in ((DocsRoutes.OPENAPI_JSON, openapi), (DocsRoutes.SWAGGER_UI, swagger_ui_html),
                                 (DocsRoutes.SWAGGER_UI_OAUTH2_REDIRECT, swagger_ui_redirect),
                                 (DocsRoutes.REDOC, redoc_html)):
            self.router.add_route(self.version.value + member.value, endpoint, include_in_schema=False)
            self.templates.add(self.version.value + member.value)

    def openapi(self, app: FastAPI) -> Dict[str, Any]:
        """The OpenAPI document of this version in app: its routes and the unversioned ones (/health), built once"""
        if app not in self._documents:
            routes = [route for route in app.routes if not isinstance(route, APIRoute) or self.owns(route.path)
                      or not any(version.owns(route.path) for version in self.published.values())]
            self._documents[app] = openapi_document(app, routes)
        return self._documents[app]

    def owns(self, path: str) -> bool:
        return path.startswith(self.version.value + "/")

    def path(self, member: Enum, **params) -> str:
        """Full path of a route enum member in this version, the template itself when no params are given.
        BASE gives the prefix of the group. LookupError if this version does not publish it."""
        group = type(member)
        full = self.version.value + group.BASE.value + ("" if member is group.BASE else member.value)
        published = full in self.templates if member is not group.BASE else any(
            template.startswith(full + "/") for template in self.templates)
        if not published:
            raise LookupError(f"{self.version.value} does not publish {group.__name__}.{member.name}")
        return full.format(**params) if params else full


def request_version(request: Request) -> PublishedVersion:
    """FastAPI dependency: the version the matched route belongs to"""
    path = request.scope["route"].path_format
    for version in PublishedVersion.published.values():
        if version.owns(path):
            return version
    raise LookupError(f"{path} belongs to no published API version")
