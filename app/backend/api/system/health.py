from fastapi import APIRouter, Depends, Request, status
from sqlmodel.ext.asyncio.session import AsyncSession
import asyncio
from loguru import logger

from app.backend.database import get_session
from app.backend.config import settings
from app.backend.enums.general import ServiceHealthStatus, ServiceUptimeStatus
from app.backend.enums.routes import SystemRoutes
from app.backend.api.shared.models.response.health_response import HealthResponse
from app.backend.utils.datetime_utils import get_current_time_in_timezone
from app.backend.api.shared.helpers.health_helper import (
    check_database_health,
    check_services_health,
    health_rate_limiter,
    is_host_allowed,
    DatabaseHealthModel,
    ServicesHealthModel,
    ServiceStatusModel
)
from app.backend.services.api_errors import ApiError
from app.backend.enums.errors import ErrorCode
from app.backend.enums.errors import AccessErrorCode

router = APIRouter(tags=[SystemRoutes.tag()])


@router.get(SystemRoutes.HEALTH.value, response_model=HealthResponse)
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

    # Check rate limiting
    if not health_rate_limiter.is_allowed(client_host):
        raise ApiError(
            ErrorCode.HEALTH_RATE_LIMITED,
            headers={"Retry-After": str(settings.health.rate_limit_window_seconds)},
            max_requests=settings.health.rate_limit_requests,
            window_seconds=settings.health.rate_limit_window_seconds,
        )

    try:
        # Run health checks concurrently
        db_health, services_health = await asyncio.gather(
            check_database_health(session),
            check_services_health()
        )

        # Determine overall status
        overall_status = ServiceHealthStatus.HEALTHY.value
        if db_health.status != ServiceHealthStatus.HEALTHY.value:
            overall_status = ServiceHealthStatus.DEGRADED.value

        return HealthResponse(
            status=overall_status,
            timestamp=get_current_time_in_timezone(settings.timezone.timezone),
            version=settings.app.version,
            environment=settings.app.environment,
            timezone=settings.timezone.timezone,
            database=db_health,
            services=services_health
        )

    except Exception as e:
        # Log the actual error for debugging (server-side only)
        logger.error(f"Health check failed for client {client_host}: {type(e).__name__}: {str(e)}")
        logger.exception("Health check exception details:")

        return HealthResponse(
            status=ServiceHealthStatus.UNHEALTHY.value,
            timestamp=get_current_time_in_timezone(settings.timezone.timezone),
            version=settings.app.version,
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
