from fastapi import APIRouter, Depends, Request
from sqlmodel.ext.asyncio.session import AsyncSession
import asyncio
from loguru import logger

from app.backend.build_info import build_info
from app.backend.database import get_session
from app.backend.config import settings
from app.backend.enums.general import ServiceHealthStatus, ServiceUptimeStatus
from app.backend.enums.routes import SystemRoutes
from app.backend.utils.datetime_utils import get_current_time_in_timezone
from app.backend.api.system.health_checks import (
    HealthResponse,
    ReadyResponse,
    SchemaHealthModel,
    readiness,
    ready_rate_limiter,
    schema_state,
    check_authorization_health,
    check_backups_health,
    check_database_health,
    check_events_health,
    check_moves_health,
    check_pki_health,
    check_services_health,
    health_rate_limiter,
    is_host_allowed,
    DatabaseHealthModel,
    ServicesHealthModel,
    ServiceStatusModel
)
from app.backend.errors.problems import ApiError
from app.backend.enums.errors import AccessErrorCode, ErrorCode
from app.backend.errors.openapi import problems

router = APIRouter(tags=[SystemRoutes.tag()])


def _client_host(request: Request) -> str:
    # uvicorn runs with --proxy-headers behind nginx: request.client is the real client
    return request.client.host if request.client else "unknown"


def _check_rate(limiter, client_host: str) -> None:
    if not limiter.is_allowed(client_host):
        raise ApiError(
            ErrorCode.HEALTH_RATE_LIMITED,
            headers={"Retry-After": str(limiter.window_seconds)},
            max_requests=limiter.max_requests,
            window_seconds=limiter.window_seconds,
        )


@router.get(
    SystemRoutes.HEALTH_READY.value,
    response_model=ReadyResponse,
    responses=problems(AccessErrorCode.HEALTH_HOST_NOT_ALLOWED, ErrorCode.HEALTH_RATE_LIMITED,
                       ErrorCode.SERVICE_NOT_READY),
)
async def health_ready(request: Request, session: AsyncSession = Depends(get_session)):
    """Whether this process may take requests: the database answers and the schema admits this release
    (RELEASE.md). 200 with the release and the schema revision, or 503 service_not_ready with reason_code
    database_unreachable | schema_out_of_range. Cached 10 s. The event hub, PKI and backups do not count
    (GET /health reports them). Same host check as /health, its own rate limit."""
    client_host = _client_host(request)
    if not is_host_allowed(client_host):
        raise ApiError(AccessErrorCode.HEALTH_HOST_NOT_ALLOWED)
    _check_rate(ready_rate_limiter, client_host)
    state = await readiness(session)
    if not state.ready:
        raise ApiError(ErrorCode.SERVICE_NOT_READY, headers={"Retry-After": "10"}, reason_code=state.reason_code)
    info = build_info()
    return ReadyResponse(ready=True, version=info.version, revision=info.revision,
                         schema_revision=state.schema.revision)


@router.get(
    SystemRoutes.HEALTH.value,
    response_model=HealthResponse,
    responses=problems(AccessErrorCode.HEALTH_HOST_NOT_ALLOWED, ErrorCode.HEALTH_RATE_LIMITED),
)
async def health_check(
    request: Request,
    session: AsyncSession = Depends(get_session)
):
    """
    Health check endpoint with rate limiting and host restrictions
    Returns system status, database connectivity, and basic statistics

    Security features:
    - Rate limiting: Configurable requests per time window
    - Host restrictions: Only allowed hosts can access (configurable)
    """
    # Get client information
    # uvicorn runs with --proxy-headers behind nginx: request.client is the real client
    client_host = request.client.host if request.client else "unknown"

    # Check host restrictions
    if not is_host_allowed(client_host):
        raise ApiError(AccessErrorCode.HEALTH_HOST_NOT_ALLOWED)

    _check_rate(health_rate_limiter, client_host)

    try:
        # Run health checks concurrently
        db_health, services_health = await asyncio.gather(
            check_database_health(session),
            check_services_health()
        )

        # After the gather: the two checks above share the session, this one uses it alone
        pki_health = await check_pki_health(session)
        backups_health = await check_backups_health()
        authorization_health = await check_authorization_health(session)
        moves_health = await check_moves_health(session)
        events_health = await check_events_health(session)
        schema = await schema_state(session)
        schema_health = SchemaHealthModel(revision=schema.revision, code_head=schema.code_head,
                                          min_code_head=schema.min_code_head, compatible=schema.compatible)

        # Determine overall status
        overall_status = ServiceHealthStatus.HEALTHY.value
        if db_health.status != ServiceHealthStatus.HEALTHY.value or (
                pki_health is not None and pki_health.status != ServiceHealthStatus.HEALTHY.value) or (
                backups_health is not None and backups_health.status != ServiceHealthStatus.HEALTHY.value) or (
                authorization_health.status != ServiceHealthStatus.HEALTHY.value) or (
                events_health is not None and events_health.status != ServiceHealthStatus.HEALTHY.value) or (
                not schema.compatible):
            overall_status = ServiceHealthStatus.DEGRADED.value

        return HealthResponse(
            status=overall_status,
            timestamp=get_current_time_in_timezone(settings.timezone.timezone),
            version=build_info().version,
            revision=build_info().revision,
            environment=settings.app.environment,
            timezone=settings.timezone.timezone,
            database=db_health,
            services=services_health,
            pki=pki_health,
            backups=backups_health,
            authorization=authorization_health,
            moves=moves_health,
            events=events_health,
            schema=schema_health,
        )

    except Exception as e:
        # Log the actual error for debugging (server-side only)
        logger.error(f"Health check failed for client {client_host}: {type(e).__name__}: {str(e)}")
        logger.exception("Health check exception details:")

        return HealthResponse(
            status=ServiceHealthStatus.UNHEALTHY.value,
            timestamp=get_current_time_in_timezone(settings.timezone.timezone),
            version=build_info().version,
            revision=build_info().revision,
            environment=settings.app.environment,
            timezone=settings.timezone.timezone,
            database=DatabaseHealthModel(
                status=ServiceHealthStatus.ERROR.value, 
                error="Service health check failed. Contact system administrator."
            ),
            services=ServicesHealthModel(
                system=ServiceStatusModel(status=ServiceHealthStatus.ERROR.value, uptime=ServiceUptimeStatus.UNKNOWN.value)
            )
        )
