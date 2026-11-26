from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.exc import OperationalError
from pydantic import Field
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
from app.backend.services.events.cleanup import oldest_row_age_seconds
from app.backend.services.events.health import hub_reports
from app.backend.services.move_queue import waiting_report
from app.backend.services.signer_client import SignerRefused, SignerUnavailable, signer_client
from app.backend.utils.datetime_utils import assume_utc
from app.backend.utils.validators import ResponseDatetime, ResponseModel
from app.backend.auth.integrity import authorization_report
from app.backend.bootstrap import schema_range

_logger = get_api_logger()


# Pydantic v2 Models for Prepare Health Check Responses

class DatabaseStatsModel(ResponseModel):
    """Database statistics model"""
    active_users: int = Field(..., description="Number of active users")
    active_posts: int = Field(..., description="Number of active posts")
    total_observations: int = Field(..., description="Total number of observations")
    active_observations: int = Field(..., description="Number of active observations")


class DatabaseHealthModel(ResponseModel):
    """Database health check response model"""
    status: str = Field(..., description="Database health status (healthy/unhealthy)")
    response_time: Optional[float] = Field(None, description="Response time in seconds")
    stats: Optional[DatabaseStatsModel] = Field(None, description="Database statistics")
    error: Optional[str] = Field(None, description="Error message if unhealthy")


class ServiceStatusModel(ResponseModel):
    """Individual service status model"""
    status: str = Field(..., description="Service status (healthy/unhealthy)")
    uptime: str = Field(..., description="Service uptime information")


class ServicesHealthModel(ResponseModel):
    """Services health check response model"""
    system: ServiceStatusModel = Field(..., description="System service status")


class PkiHealthModel(ResponseModel):
    """Client certificate authority health (only when settings.pki is configured)"""
    status: str = Field(..., description="healthy, or degraded: less than 2 days left on the CA or a CRL, a CRL "
                                         "never published, a stale CRL or no signer")
    ca_expires_in_days: int = Field(..., description="Whole days until the client CA certificate expires")
    crl_next_update_in_days: Optional[int] = Field(
        None, description="Whole days until the earliest next_update of every CRL in the published file; null when "
                          "the file is missing")
    crl_stale: bool = Field(..., description="A revocation newer than the published CRL")
    signer_available: bool = Field(..., description="The signing service answers")


class BackupsHealthModel(ResponseModel):
    """Database backups (only when settings.backup is configured)"""
    status: str = Field(..., description="healthy, or degraded: no good backup for backup.stale_after_hours, the last "
                                         "run failed, the newest backup has no off-host copy (with a copy "
                                         "directory), or the backup directory cannot be read")
    last_success_at: Optional[ResponseDatetime] = Field(None, description="End of the newest complete backup")
    stale: bool = Field(..., description="No complete backup newer than backup.stale_after_hours")
    last_run_outcome: Optional[str] = Field(None, description="ok or failed: the last backup run")


class AuthorizationHealthModel(ResponseModel):
    """Authorization (auth/integrity.py), counts only"""
    status: str = Field(..., description="healthy, or degraded: a grant puts less in effect than its role holds, or "
                                         "no effective administrator")
    violations: int = Field(..., description="Grants that leave permissions out (level or type boundary)")
    system_administrators: int = Field(
        ..., description="Effective administrators: active, not archived users holding the built-in "
                         "system_administrator system role and administrator at the root, whose user type puts both "
                         "in effect and, where login requires a client certificate (every environment but "
                         "development), with an active certificate")


class MovesHealthModel(ResponseModel):
    """Moves that wait for the uploads below them (services/moves.py), counts only"""
    waiting: int = Field(..., description="Moves waiting now")
    oldest_waiting_seconds: Optional[int] = Field(None, description="Seconds the oldest of them has waited; null: none")


class EventsHealthModel(ResponseModel):
    """Real-time events (only when settings.events.enabled): the sum of the hubs that reported within 60 s"""
    status: str = Field(..., description="healthy, or degraded: no hub running reported within 60 s, a worker's "
                                         "hub is down, or the event lag is above 30 s")
    workers: int = Field(..., description="Hubs (api worker processes serving streams) that reported within 60 s")
    workers_down: int = Field(..., description="Api worker processes that should serve streams but whose hub failed "
                                               "to start, reported within 60 s: the start is retried, and their "
                                               "streams answer 503 meanwhile; the log has the error")
    streams: int = Field(..., description="Open streams, all hubs")
    lag_ms: int = Field(..., description="The highest p95 of the hubs: database clock at read minus occurred_at")
    gaps: int = Field(..., description="Ids the hubs still watch for a late commit, all hubs")
    slow_closed: int = Field(..., description="Streams closed for not reading, since each hub started")
    reloads: int = Field(..., description="reload messages sent, since each hub started")
    oldest_row_age_seconds: Optional[int] = Field(None, description="Age of the oldest row of outbox_events; null "
                                                                    "when the table is empty")


class SchemaHealthModel(ResponseModel):
    """The database schema against this release (RELEASE.md, "Release rules for developers")"""
    revision: Optional[str] = Field(..., description="Migration revision of the database; null before the first")
    code_head: str = Field(..., description="Newest migration revision this release knows")
    min_code_head: Optional[str] = Field(..., description="Oldest code head the schema admits (schema_compat); null: "
                                                          "no contract migration since the baseline")
    compatible: bool = Field(..., description="This release runs on the schema")


class HealthResponse(ResponseModel):
    """Main health check response model"""
    status: str = Field(..., description="Overall system status")
    timestamp: ResponseDatetime = Field(..., description="Current server timestamp")
    version: str = Field(..., description="Release tag of the running image (YYYY.MM.DD-sha7), dev outside an image")
    revision: Optional[str] = Field(..., description="Commit the image was built from; null outside an image")
    environment: str = Field(..., description="Current environment")
    timezone: str = Field(..., description="Server timezone")
    database: DatabaseHealthModel = Field(..., description="Database health information")
    services: ServicesHealthModel = Field(..., description="Services health information")
    pki: Optional[PkiHealthModel] = Field(None, description="Client certificate authority; only when configured")
    backups: Optional[BackupsHealthModel] = Field(None, description="Database backups; only when configured")
    authorization: Optional[AuthorizationHealthModel] = Field(None, description="Authorization data, counts only")
    moves: Optional[MovesHealthModel] = Field(None, description="Moves waiting for uploads, counts only")
    events: Optional[EventsHealthModel] = Field(None, description="Real-time events, counts only; only when they are on")
    schema_: Optional[SchemaHealthModel] = Field(None, alias="schema", serialization_alias="schema",
                                                 description="The database schema against this release")


# Rate limiting for health endpoint
class HealthRateLimiter:
    """Simple rate limiter of a health endpoint; the limits are the named settings of settings.health"""

    def __init__(self, requests_setting: str = "rate_limit_requests",
                 window_setting: str = "rate_limit_window_seconds"):
        self.requests: dict[str, list[float]] = defaultdict(list)
        self.requests_setting = requests_setting
        self.window_setting = window_setting

    @property
    def max_requests(self) -> int:
        return getattr(settings.health, self.requests_setting)

    @property
    def window_seconds(self) -> int:
        return getattr(settings.health, self.window_setting)

    def is_allowed(self, client_ip: str) -> bool:
        """Check if request is allowed based on rate limit from config"""
        if not settings.health.enable_rate_limit:
            return True

        current_time = time.time()
        window = self.window_seconds
        max_requests = self.max_requests

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
        window = self.window_seconds

        for ip in list(self.requests.keys()):
            self.requests[ip] = [
                timestamp for timestamp in self.requests[ip]
                if timestamp > current_time - window
            ]
            if not self.requests[ip]:
                del self.requests[ip]


# Global rate limiter instance for health endpoint
health_rate_limiter = HealthRateLimiter()
# GET /health/ready: its own bucket, so the healthcheck never uses up /health and the other way round
ready_rate_limiter = HealthRateLimiter("ready_rate_limit_requests", "ready_rate_limit_window_seconds")


# ---------------------------------------------------------------- schema and readiness (RELEASE.md)

READINESS_CACHE_SECONDS = 10


@dataclass(frozen=True)
class SchemaState:
    revision: Optional[str]
    min_code_head: Optional[str]
    code_head: str
    compatible: bool


@dataclass(frozen=True)
class Readiness:
    ready: bool
    reason_code: Optional[str]          # database_unreachable | schema_out_of_range
    schema: Optional[SchemaState]


_readiness_cache: dict = {}


def reset_readiness_cache() -> None:
    _readiness_cache.clear()


async def schema_state(session: AsyncSession) -> SchemaState:
    """R, M and H of the database this process uses, and whether the api of this release runs on it (the rule of
    bootstrap/schema_range.py)"""
    conn = await session.connection()
    revision, min_code_head = await conn.run_sync(
        lambda sync: (schema_range.read_revision(sync), schema_range.read_min_code_head(sync)))
    verdict = schema_range.decide("api", revision, min_code_head)
    return SchemaState(revision=revision, min_code_head=min_code_head, code_head=verdict.code_head,
                       compatible=verdict.run)


async def readiness(session: AsyncSession) -> Readiness:
    """Database reachable and schema in range, cached READINESS_CACHE_SECONDS (the event hub, PKI and backups do not
    count: a process without them still serves)"""
    now = time.monotonic()
    cached = _readiness_cache.get("value")
    if cached is not None and now - _readiness_cache["at"] < READINESS_CACHE_SECONDS:
        return cached
    try:
        state = await schema_state(session)
    except (OperationalError, OSError) as exc:
        _logger.warning(f"Readiness: database unreachable ({type(exc).__name__})")
        result = Readiness(ready=False, reason_code="database_unreachable", schema=None)
    else:
        result = Readiness(ready=state.compatible, reason_code=None if state.compatible else "schema_out_of_range",
                           schema=state)
    _readiness_cache.update(value=result, at=now)
    return result


class ReadyResponse(ResponseModel):
    """GET /health/ready when this process may take requests"""
    ready: bool = Field(..., description="Always true in a 200 answer")
    version: str = Field(..., description="Release tag of the running image (YYYY.MM.DD-sha7), dev outside an image")
    revision: Optional[str] = Field(..., description="Commit the image was built from; null outside an image")
    schema_revision: Optional[str] = Field(..., description="Migration revision of the database")


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


async def check_authorization_health(session: AsyncSession) -> AuthorizationHealthModel:
    """The authorization part of /health: the counts of auth/integrity.authorization_report"""
    report = await authorization_report(session)
    healthy = not report.violations and report.system_administrators > 0
    return AuthorizationHealthModel(
        status=(ServiceHealthStatus.HEALTHY if healthy else ServiceHealthStatus.DEGRADED).value,
        violations=len(report.violations), system_administrators=report.system_administrators,
    )


async def check_moves_health(session: AsyncSession) -> MovesHealthModel:
    """The moves part of /health: how many wait and for how long"""
    report = await waiting_report(session)
    if report.oldest_requested_at is None:
        return MovesHealthModel(waiting=report.count)
    age = datetime.now(timezone.utc) - assume_utc(report.oldest_requested_at)
    return MovesHealthModel(waiting=report.count, oldest_waiting_seconds=max(0, int(age.total_seconds())))


EVENTS_REPORT_SECONDS = 60      # a hub reports every 15 s
EVENTS_LAG_LIMIT_MS = 30000


async def check_events_health(session: AsyncSession) -> Optional[EventsHealthModel]:
    """The events part of /health, None when events are off. Read from the table: a request lands on one worker of
    four and knows only its own hub"""
    if not settings.events.enabled:
        return None
    reports = await hub_reports(session, EVENTS_REPORT_SECONDS)
    running = [report for report in reports if not report.get("down")]
    down = len(reports) - len(running)
    lag_ms = max((report.get("lag_ms", 0) for report in running), default=0)
    healthy = bool(running) and not down and lag_ms <= EVENTS_LAG_LIMIT_MS
    return EventsHealthModel(
        status=(ServiceHealthStatus.HEALTHY if healthy else ServiceHealthStatus.DEGRADED).value,
        workers=len(running), workers_down=down, lag_ms=lag_ms,
        **{name: sum(report.get(name, 0) for report in running) for name in ("streams", "gaps", "slow_closed", "reloads")},
        oldest_row_age_seconds=await oldest_row_age_seconds(session))


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
