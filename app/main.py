import asyncio
import sys
import os
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Add project root to Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from app.backend.config import settings
from app.backend.utils.logging_config import setup_logging, get_logger, get_main_logger
from app.backend.database import initialize_database_with_migrations
from app.backend.services.route_access import build_route_access_map
from app.backend.error_handlers import register_error_handlers
from app.backend.openapi_problems import install_openapi
from app.backend.api import v1
from app.backend.api.system import health, problems
from app.backend.enums.routes import AuthRoutes, DocsRoutes, SystemRoutes

# Initialize logging system
setup_logging(
    log_file_path=settings.logging.file_path if settings.logging.enable_file_logging else None,
    log_level=settings.logging.level,
    environment=settings.app.environment
)

# Get application logger
_logger = get_main_logger()

API_DESCRIPTION = f"""
Radio monitoring API. This page, `{DocsRoutes.REDOC.path()}` and `{DocsRoutes.OPENAPI_JSON.path()}` are generated
from the code, errors included.

## Authentication
`Authorization: Bearer <access token>` from `POST {AuthRoutes.LOGIN.versioned_path()}`. A 401 with `invalid_token` or
`session_expired` means: refresh with `POST {AuthRoutes.REFRESH.versioned_path()}`; any other 401 code means: log in again.

## Errors (RFC 9457 Problem Details)
Every error answers `Content-Type: application/problem+json`:

| member | meaning |
|---|---|
| `type` | URI of the problem type; open it for its documentation (`{SystemRoutes.PROBLEMS.path()}/<code>`) |
| `title` | fixed summary of the problem type |
| `status` | HTTP status code |
| `detail` | English text of this occurrence |
| `instance` | path of the request that failed |
| `code` | **localization key** of exactly this message (never a category) |
| other members | params of the message (`post_id`, `offset`, `max_file_size_bytes`, ...) |

Each response of each operation lists the problem types it can answer (`oneOf`, discriminator `code`).
`components.schemas.ProblemCode` lists every code, `{SystemRoutes.PROBLEMS.path()}` documents them all.

`validation_error` (422) carries `errors`: one item per invalid value with `code`, `detail` and either
`pointer` (JSON Pointer into the body, `#/username`) or `parameter` + `location` (query, path, header, cookie).
Item codes are `components.schemas.FieldErrorCode` plus pydantic's codes (`missing`, `string_too_short`, ...).

## Typed client
`npx openapi-typescript <origin>{DocsRoutes.OPENAPI_JSON.path()} -o src/api/schema.ts` generates the types; operation ids
are the endpoint names (`update_post`).
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle"""
    try:
        _logger.info("Starting application...")
        await initialize_database_with_migrations()
        _logger.info("Database initialization completed. Server ready.")
        yield
        _logger.info("Shutting down application...")
    except Exception as e:
        _logger.error(f"Startup failed: {e}")
        import traceback
        traceback.print_exc()
        raise


fastapi_app = FastAPI(
    title=settings.app.name,
    version=settings.app.version,
    description=API_DESCRIPTION,
    lifespan=lifespan,
    openapi_url=DocsRoutes.OPENAPI_JSON.path(),
    docs_url=DocsRoutes.SWAGGER_UI.path(),
    swagger_ui_oauth2_redirect_url=DocsRoutes.SWAGGER_UI_OAUTH2_REDIRECT.path(),
    redoc_url=DocsRoutes.REDOC.path(),
    # operationId = the endpoint function name (update_post), what client generators name their functions after;
    # tests/test_openapi.py keeps them unique
    generate_unique_id_function=lambda route: route.name,
)

# Every error is an RFC 9457 problem (application/problem+json), FastAPI's own 422/404/405/500 included
register_error_handlers(fastapi_app)

# Add CORS middleware (must be before other middleware)
fastapi_app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.security.allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"],
    allow_headers=["*"],
    max_age=600
)

# Include routers: each one carries its prefix and tag, the version router adds /api/v1
fastapi_app.include_router(health.router)
fastapi_app.include_router(v1.router)
# Documentation of the problem types (the "type" URI of every error); not part of the OpenAPI schema
fastapi_app.include_router(problems.router)

# OpenAPI lists, per route and status, every problem type the route can answer (openapi_problems.py)
install_openapi(fastapi_app)

# Route access map: generated from the require() dependencies of every router above.
# Routers included after this line would be missing from /auth/route-access.
fastapi_app.state.route_access_map = build_route_access_map(fastapi_app)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:fastapi_app",
        host=settings.server.host,
        port=settings.server.port,
        reload=settings.server.reload,
        log_level=settings.logging.level.lower()
    )
