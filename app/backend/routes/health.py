from fastapi import APIRouter, Depends, Request, HTTPException, status
from sqlmodel.ext.asyncio.session import AsyncSession
import asyncio

from app.backend.database import get_session
from app.backend.config import settings
from app.backend.enums.general import ServiceHealthStatus, ServiceUptimeStatus
from app.backend.enums.routes import SystemRoutes
from app.backend.routes.models.response.health_response import HealthResponse
from app.backend.utils.datetime_utils import get_current_time_in_timezone
from app.backend.routes.helpers.health_helper import (
    check_database_health,
    check_services_health,
    health_rate_limiter,
    is_host_allowed,
    DatabaseHealthModel,
    ServicesHealthModel,
    ServiceStatusModel
)
from app.backend.helpers.ip_request_helper import get_real_client_ip

router = APIRouter()


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
    client_ip = get_real_client_ip(request)
    client_host = request.client.host if request.client else "unknown"

    # Check host restrictions
    if not is_host_allowed(client_host):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access denied: Host {client_host} is not allowed to access health endpoint"
        )

    # Check rate limiting
    if not health_rate_limiter.is_allowed(client_ip):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded. Maximum {settings.health.rate_limit_requests} requests per {settings.health.rate_limit_window_seconds} seconds",
            headers={"Retry-After": str(settings.health.rate_limit_window_seconds)}
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
        # Return error response with Pydantic models
        return HealthResponse(
            status=ServiceHealthStatus.UNHEALTHY.value,
            timestamp=get_current_time_in_timezone(settings.timezone.timezone),
            version=settings.app.version,
            environment=settings.app.environment,
            timezone=settings.timezone.timezone,
            database=DatabaseHealthModel(status=ServiceHealthStatus.ERROR.value, error=str(e)),
            services=ServicesHealthModel(
                system=ServiceStatusModel(status=ServiceHealthStatus.ERROR.value, uptime=ServiceUptimeStatus.UNKNOWN.value)
            )
        )
