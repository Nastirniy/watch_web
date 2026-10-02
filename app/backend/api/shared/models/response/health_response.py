from datetime import datetime

from pydantic import BaseModel, Field

from app.backend.api.shared.helpers.health_helper import DatabaseHealthModel, ServicesHealthModel


class HealthResponse(BaseModel):
    """Main health check response model"""
    status: str = Field(..., description="Overall system status")
    timestamp: datetime = Field(..., description="Current server timestamp")
    version: str = Field(..., description="Application version")
    environment: str = Field(..., description="Current environment")
    timezone: str = Field(..., description="Server timezone")
    database: DatabaseHealthModel = Field(..., description="Database health information")
    services: ServicesHealthModel = Field(..., description="Services health information")
