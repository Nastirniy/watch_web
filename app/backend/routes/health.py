from fastapi import APIRouter, Depends
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlmodel import select, func
from datetime import datetime, timezone
from pydantic import BaseModel
from typing import Dict, Any
import asyncio

from app.backend.database import get_session
from app.backend.config import settings
from app.backend.enums.routes import SystemRoutes
from app.backend.models.user import User
from app.backend.models.post import Post
from app.backend.models.observation import Observation
from app.backend.utils.datetime_utils import get_current_time_in_timezone

router = APIRouter()


class HealthResponse(BaseModel):
    status: str
    timestamp: datetime
    version: str
    environment: str
    timezone: str
    database: Dict[str, Any]
    services: Dict[str, Any]


async def check_database_health(session: AsyncSession) -> Dict[str, Any]:
    """Check database connectivity and basic stats"""
    try:
        start_time = datetime.now(timezone.utc)

        # Basic connectivity test using ORM
        await session.exec(select(func.count()).select_from(User).limit(1))

        # Get basic stats using ORM queries
        active_users_result = await session.exec(
            select(func.count(User.id)).where(User.is_deleted is False)
        )
        active_users = active_users_result.one()

        active_posts_result = await session.exec(
            select(func.count(Post.id)).where(Post.is_active == True)
        )
        active_posts = active_posts_result.one()

        total_observations_result = await session.exec(
            select(func.count(Observation.id)).where(Observation.is_deleted is False)
        )
        total_observations = total_observations_result.one()

        active_observations_result = await session.exec(
            select(func.count(Observation.id)).where(
                (Observation.status == 'active') & (Observation.is_deleted is False)
            )
        )
        active_observations = active_observations_result.one()

        response_time = (datetime.now(timezone.utc) - start_time).total_seconds()

        return {
            "status": "healthy",
            "response_time": round(response_time, 4),
            "stats": {
                "active_users": active_users,
                "active_posts": active_posts,
                "total_observations": total_observations,
                "active_observations": active_observations
            }
        }
    except Exception as e:
        return {
            "status": "unhealthy",
            "error": str(e),
            "response_time": None,
            "stats": None
        }


async def check_services_health() -> Dict[str, Any]:
    """Check external services health"""
    services = {}

    # Add any external service checks here
    # For now, we'll just return system info
    services["system"] = {
        "status": "healthy",
        "uptime": "running"
    }

    return services


@router.get(SystemRoutes.HEALTH.value, response_model=HealthResponse)
async def health_check(session: AsyncSession = Depends(get_session)):
    """
    Health check endpoint
    Returns system status, database connectivity, and basic statistics
    """
    try:
        # Run health checks concurrently
        db_health, services_health = await asyncio.gather(
            check_database_health(session),
            check_services_health()
        )

        # Determine overall status
        overall_status = "healthy"
        if db_health.get("status") != "healthy":
            overall_status = "degraded"

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
        return HealthResponse(
            status="unhealthy",
            timestamp=get_current_time_in_timezone(settings.timezone.timezone),
            version=settings.app.version,
            environment=settings.app.environment,
            timezone=settings.timezone.timezone,
            database={"status": "error", "error": str(e)},
            services={"status": "error", "error": str(e)}
        )