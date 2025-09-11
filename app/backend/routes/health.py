from fastapi import APIRouter, Depends
from sqlmodel.ext.asyncio.session import AsyncSession
from sqlalchemy import text
from datetime import datetime, timezone
from pydantic import BaseModel
from typing import Dict, Any
import asyncio

from app.backend.database import get_session
from app.backend.config import settings
from app.backend.enums.routes import SystemRoutes

router = APIRouter()


class HealthResponse(BaseModel):
    status: str
    timestamp: datetime
    version: str
    environment: str
    database: Dict[str, Any]
    services: Dict[str, Any]


async def check_database_health(session: AsyncSession) -> Dict[str, Any]:
    """Check database connectivity and basic stats"""
    try:
        start_time = datetime.now(timezone.utc)

        # Basic connectivity test
        await session.exec(text("SELECT 1"))

        # Get basic stats
        result = await session.exec(text("""
            SELECT 
                (SELECT COUNT(*) FROM users WHERE is_deleted = FALSE) as active_users,
                (SELECT COUNT(*) FROM posts WHERE is_active = TRUE) as active_posts,
                (SELECT COUNT(*) FROM observations WHERE is_deleted = FALSE) as total_observations,
                (SELECT COUNT(*) FROM observations WHERE status = 'active' AND is_deleted = FALSE) as active_observations
        """))
        stats = result.fetchone()

        response_time = (datetime.now(timezone.utc) - start_time).total_seconds()

        return {
            "status": "healthy",
            "response_time": round(response_time, 4),
            "stats": {
                "active_users": stats[0] if stats else 0,
                "active_posts": stats[1] if stats else 0,
                "total_observations": stats[2] if stats else 0,
                "active_observations": stats[3] if stats else 0
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
            timestamp=datetime.now(timezone.utc),
            version=settings.app.version,
            environment=settings.app.environment,
            database=db_health,
            services=services_health
        )

    except Exception as e:
        return HealthResponse(
            status="unhealthy",
            timestamp=datetime.now(timezone.utc),
            version=settings.app.version,
            environment=settings.app.environment,
            database={"status": "error", "error": str(e)},
            services={"status": "error", "error": str(e)}
        )