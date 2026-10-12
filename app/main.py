from contextlib import asynccontextmanager
from fastapi import FastAPI

from app.backend.config import settings
from app.backend.utils.logging_config import setup_logging, get_logger, get_main_logger
from app.backend.bootstrap.deployment import check_deployment
from app.backend.bootstrap.pki import check_pki_settings, publish_crl_at_startup
from app.backend.bootstrap.startup import initialize_database_with_migrations
from app.backend.errors.handlers import register_error_handlers
from app.backend.request_id import RequestIdMiddleware
from app.backend.enums.errors import ErrorCode
from app.backend.errors.openapi import problems
from app.backend.api.v1 import V1
from app.backend.api.system import health, problems as problem_pages
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
Radio monitoring API. Every API version has its own OpenAPI document, generated from the code, errors included,
with Swagger UI and ReDoc next to it: `{V1.path(DocsRoutes.OPENAPI_JSON)}`, `{V1.path(DocsRoutes.SWAGGER_UI)}`,
`{V1.path(DocsRoutes.REDOC)}`.

## Authentication
`Authorization: Bearer <access token>` from `POST {V1.path(AuthRoutes.LOGIN)}`. A 401 with `invalid_token` means:
refresh with `POST {V1.path(AuthRoutes.REFRESH)}`; any other 401 code (`session_expired` included: the session
ended) means: log in again. A session lives while it is used and is bound to the client certificate it was
opened with.

## Errors (RFC 9457 Problem Details)
Every error answers `Content-Type: application/problem+json`:

| member | meaning |
|---|---|
| `type` | URI of the problem type; open it for its documentation (`{SystemRoutes.PROBLEMS.value}/<code>`) |
| `title` | fixed summary of the problem type |
| `status` | HTTP status code |
| `detail` | English text of this occurrence |
| `instance` | path of the request that failed |
| `code` | **localization key** of exactly this message (never a category) |
| other members | params of the message (`post_id`, `offset`, `max_file_size_bytes`, ...) |

Each response of each operation lists the problem types it can answer (`oneOf`, discriminator `code`).
Each problem type is a component schema `<Code>Problem` (`PostNotFoundProblem`); `{SystemRoutes.PROBLEMS.value}` documents them all.

`validation_error` (422) carries `errors`: one item per invalid value with `code`, `detail` and either
`pointer` (JSON Pointer into the body, `#/username`) or `parameter` + `location` (query, path, header, cookie).
Item codes are `components.schemas.FieldErrorCode` plus pydantic's codes (`missing`, `string_too_short`, ...).

## Typed client
`npx openapi-typescript <origin>{V1.path(DocsRoutes.OPENAPI_JSON)} -o src/api/schema.ts` generates the types of v1;
operation ids are the endpoint names (`update_post`).
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle"""
    try:
        check_deployment(settings)
        check_pki_settings(settings.pki)
        _logger.info("Starting application...")
        await initialize_database_with_migrations()
        await publish_crl_at_startup()
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
    # No document at the root: each API version serves its own (/api/v1/openapi.json, /docs, /redoc), so models
    # of two versions never share or rename each other's components (api/versioning.py)
    openapi_url=None,
    docs_url=None,
    redoc_url=None,
    # Any route may fail unexpectedly: 500 internal_error is documented once for all of them
    responses=problems(ErrorCode.INTERNAL_ERROR),
    # operationId = the endpoint function name (update_post), what client generators name their functions after;
    # tests/test_openapi.py keeps them unique within a version
    generate_unique_id_function=lambda route: route.name,
)

# Every error is an RFC 9457 problem (application/problem+json), FastAPI's own 422/404/405/500 included
register_error_handlers(fastapi_app)
# Added last, so the outermost: every response and every log line of a request carries its X-Request-ID
fastapi_app.add_middleware(RequestIdMiddleware)

# No CORS here: the web client is served by nginx from the same origin as the API, and a browser client on
# another origin gets CORS at the nginx edge, where nginx's own errors get the headers too (DEPLOYMENT.md)

# Include routers: each module carries its prefix and tag; a published version adds its prefix (api/vN)
fastapi_app.include_router(health.router)
fastapi_app.include_router(V1.router)
# Documentation of the problem types (the "type" URI of every error); not part of the OpenAPI schema
fastapi_app.include_router(problem_pages.router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:fastapi_app",
        host=settings.server.host,
        port=settings.server.port,
        reload=settings.server.reload,
        log_level=settings.logging.level.lower(),
        # Logging is already set up above (uvicorn's loggers go to loguru); uvicorn's default LOGGING_CONFIG would
        # drop the handler of uvicorn.error and its startup messages (tests/test_server_logging.py)
        log_config=None,
    )
