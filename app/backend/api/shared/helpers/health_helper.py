from datetime import datetime, timezone
from typing import Optional
from pydantic import BaseModel, Field
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select, func
import time
from collections import defaultdict

from app.backend.enums.general import ServiceHealthStatus, ObservationStatus, ServiceUptimeStatus
from app.backend.models.user import User
from app.backend.models.post import Post
from app.backend.models.observation import Observation
from app.backend.config import settings
from app.backend.utils.logging_config import get_api_logger

_logger = get_api_logger()


# Pydantic v2 Models for Prepare Health Check Responses

class DatabaseStatsModel(BaseModel):
    """Database statistics model"""
    active_users: int = Field(..., description="Number of active users")
    active_posts: int = Field(..., description="Number of active posts")
    total_observations: int = Field(..., description="Total number of observations")
    active_observations: int = Field(..., description="Number of active observations")


class DatabaseHealthModel(BaseModel):
    """Database health check response model"""
    status: str = Field(..., description="Database health status (healthy/unhealthy)")
    response_time: Optional[float] = Field(None, description="Response time in seconds")
    stats: Optional[DatabaseStatsModel] = Field(None, description="Database statistics")
    error: Optional[str] = Field(None, description="Error message if unhealthy")


class ServiceStatusModel(BaseModel):
    """Individual service status model"""
    status: str = Field(..., description="Service status (healthy/unhealthy)")
    uptime: str = Field(..., description="Service uptime information")


class ServicesHealthModel(BaseModel):
    """Services health check response model"""
    system: ServiceStatusModel = Field(..., description="System service status")


# Rate limiting for health endpoint
class HealthRateLimiter:
    """Simple rate limiter specifically for health endpoint"""

    def __init__(self):
        self.requests: dict[str, list[float]] = defaultdict(list)

    def is_allowed(self, client_ip: str) -> bool:
        """Check if request is allowed based on rate limit from config"""
        if not settings.health.enable_rate_limit:
            return True

        current_time = time.time()
        window = settings.health.rate_limit_window_seconds
        max_requests = settings.health.rate_limit_requests

        # Remove timestamps older than window
        self.requests[client_ip] = [
            timestamp for timestamp in self.requests[client_ip]
            if timestamp > current_time - window
        ]

        # Check limit
        if len(self.requests[client_ip]) >= max_requests:
            return False

        # Add current request
        self.requests[client_ip].append(current_time)
        return True

    def cleanup(self) -> None:
        """Clean up old entries from all IPs"""
        current_time = time.time()
        window = settings.health.rate_limit_window_seconds

        for ip in list(self.requests.keys()):
            self.requests[ip] = [
                timestamp for timestamp in self.requests[ip]
                if timestamp > current_time - window
            ]
            if not self.requests[ip]:
                del self.requests[ip]


# Global rate limiter instance for health endpoint
health_rate_limiter = HealthRateLimiter()


def is_host_allowed(host: str) -> bool:
    """
    Check if host is in allowed list from configuration

    Args:
        host: The host to check

    Returns:
        bool: True if host is allowed
    """
    if not settings.health.enable_host_check:
        return True

    allowed_hosts = settings.health.allowed_hosts

    if not allowed_hosts:
        return True

    if "*" in allowed_hosts:
        return True

    # Check exact match
    return host in allowed_hosts


async def check_database_health(session: AsyncSession) -> DatabaseHealthModel:
    """
    Check database connectivity and basic stats

    Args:
        session: Database session

    Returns:
        DatabaseHealthModel: Database health information
    """
    try:
        start_time = datetime.now(timezone.utc)

        # Basic connectivity test using ORM
        await session.exec(select(func.count()).select_from(User).limit(1))

        # Get basic stats using ORM queries
        active_users_result = await session.exec(
            select(func.count(User.id)).where(User.is_deleted == False)
        )
        active_users = active_users_result.one()

        active_posts_result = await session.exec(
            select(func.count(Post.id)).where(Post.is_active == True)
        )
        active_posts = active_posts_result.one()

        total_observations_result = await session.exec(
            select(func.count(Observation.id)).where(Observation.is_deleted == False)
        )
        total_observations = total_observations_result.one()

        active_observations_result = await session.exec(
            select(func.count(Observation.id)).where(
                (Observation.status == ObservationStatus.ACTIVE.value) & (Observation.is_deleted == False)
            )
        )
        active_observations = active_observations_result.one()

        response_time = (datetime.now(timezone.utc) - start_time).total_seconds()

        return DatabaseHealthModel(
            status=ServiceHealthStatus.HEALTHY.value,
            response_time=round(response_time, 4),
            stats=DatabaseStatsModel(
                active_users=active_users,
                active_posts=active_posts,
                total_observations=total_observations,
                active_observations=active_observations
            )
        )
    except Exception as e:
        # A health check reports a failing database instead of failing itself. The error text is a server
        # detail: logged here, never part of the response
        _logger.opt(exception=e).error(f"Health check: database check failed: {e!r}")
        return DatabaseHealthModel(
            status=ServiceHealthStatus.UNHEALTHY.value,
            error="Server side error",
            response_time=None,
            stats=None
        )


async def check_services_health() -> ServicesHealthModel:
    """
    Check external services health

    Returns:
        ServicesHealthModel: Services health information
    """
    # Add any external service checks here
    # For now, we'll just return system info
    return ServicesHealthModel(
        system=ServiceStatusModel(
            status=ServiceHealthStatus.HEALTHY.value,
            uptime=ServiceUptimeStatus.RUNNING.value,
        )
    )

