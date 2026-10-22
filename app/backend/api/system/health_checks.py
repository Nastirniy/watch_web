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
from app.backend.pki.authority import CaCheckError, load_ca_certificate
from app.backend.errors.coded import CodedError
from app.backend.services.backups.catalog import load_catalog_async
from app.backend.services.crl_publisher import crl_status
from app.backend.services.signer_client import SignerRefused, SignerUnavailable, signer_client
from app.backend.utils.validators import ResponseDatetime

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


class PkiHealthModel(BaseModel):
    """Client certificate authority health (only when settings.pki is configured)"""
    status: str = Field(..., description="healthy, or degraded: less than 2 days left on the CA or a CRL, a CRL "
                                         "never published, a stale CRL or no signer")
    ca_expires_in_days: int = Field(..., description="Whole days until the client CA certificate expires")
    crl_next_update_in_days: Optional[int] = Field(
        None, description="Whole days until the earliest next_update of every CRL in the published file; null when "
                          "the file is missing")
    crl_stale: bool = Field(..., description="A revocation newer than the published CRL")
    signer_available: bool = Field(..., description="The signing service answers")


class BackupsHealthModel(BaseModel):
    """Database backups (only when settings.backup is configured)"""
    status: str = Field(..., description="healthy, or degraded: no good backup for backup.stale_after_hours, the last "
                                         "run failed, the newest backup has no off-host copy (with a copy "
                                         "directory), or the backup directory cannot be read")
    last_success_at: Optional[ResponseDatetime] = Field(None, description="End of the newest complete backup")
    stale: bool = Field(..., description="No complete backup newer than backup.stale_after_hours")
    last_run_outcome: Optional[str] = Field(None, description="ok or failed: the last backup run")


class HealthResponse(BaseModel):
    """Main health check response model"""
    status: str = Field(..., description="Overall system status")
    timestamp: ResponseDatetime = Field(..., description="Current server timestamp")
    version: str = Field(..., description="Application version")
    environment: str = Field(..., description="Current environment")
    timezone: str = Field(..., description="Server timezone")
    database: DatabaseHealthModel = Field(..., description="Database health information")
    services: ServicesHealthModel = Field(..., description="Services health information")
    pki: Optional[PkiHealthModel] = Field(None, description="Client certificate authority; only when configured")
    backups: Optional[BackupsHealthModel] = Field(None, description="Database backups; only when configured")


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



PKI_MIN_DAYS = 2


async def check_pki_health(session: AsyncSession) -> Optional[PkiHealthModel]:
    """The PKI part of /health, None when settings.pki is not configured"""
    if not settings.pki.is_configured():
        return None
    now = datetime.now(timezone.utc)
    try:
        ca_expires_in_days = (load_ca_certificate(settings.pki.ca_cert_path).not_valid_after_utc - now).days
    except CaCheckError as e:
        _logger.error(f"Health check: the client CA certificate is not usable: {e.reason}")
        ca_expires_in_days = 0
    status = await crl_status(session)
    next_update_in_days = None if status.earliest_next_update is None else (status.earliest_next_update - now).days
    try:
        await signer_client().health()
        signer_available = True
    except (SignerUnavailable, SignerRefused) as e:
        _logger.warning(f"Health check: the signer is not available: {type(e).__name__}")
        signer_available = False
    healthy = (ca_expires_in_days >= PKI_MIN_DAYS and next_update_in_days is not None
               and next_update_in_days >= PKI_MIN_DAYS and not status.stale and signer_available)
    return PkiHealthModel(
        status=(ServiceHealthStatus.HEALTHY if healthy else ServiceHealthStatus.DEGRADED).value,
        ca_expires_in_days=ca_expires_in_days, crl_next_update_in_days=next_update_in_days,
        crl_stale=status.stale, signer_available=signer_available)


async def check_backups_health() -> Optional[BackupsHealthModel]:
    """The backups part of /health, None when settings.backup is not configured (reads files only)"""
    if not settings.backup.is_configured():
        return None
    try:
        catalog = await load_catalog_async(settings.backup, datetime.now(timezone.utc))
    except CodedError:
        # backup_directory_unavailable (unreadable or not answering): logged where it was found
        return BackupsHealthModel(status=ServiceHealthStatus.DEGRADED.value, last_success_at=None, stale=True,
                                  last_run_outcome=None)
    summary = catalog.summary
    outcome = None if summary.last_run is None else summary.last_run.outcome
    newest_not_copied = bool(catalog.entries) and catalog.entries[0].copied is not True and summary.copy.configured
    healthy = not summary.stale and outcome != "failed" and not newest_not_copied
    return BackupsHealthModel(
        status=(ServiceHealthStatus.HEALTHY if healthy else ServiceHealthStatus.DEGRADED).value,
        last_success_at=summary.last_success_at, stale=summary.stale, last_run_outcome=outcome)
