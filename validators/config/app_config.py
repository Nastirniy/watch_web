from typing import Optional

from loguru import logger
from pydantic import Field, field_validator, ConfigDict, field_serializer
from pydantic_settings import BaseSettings
from packaging.version import Version, InvalidVersion

from enums.environment_alias import Environment
from validators.config.fastapi_config import ServerConfig, CORSConfig
from validators.config.redis_config import RedisConfig
from validators.config.sql_alchemy_config import DatabaseConfig


class AppConfig(BaseSettings):
    """Main FastAPI application configuration."""
    model_config = ConfigDict(
        str_strip_whitespace=True,
        validate_assignment=True,
        extra="forbid"
    )

    # App metadata
    app_name: str = Field(default="FastAPI App", description="Application name")
    version: Version = Field(..., description="Application version (semver)")
    description: Optional[str] = Field(default=None, description="Application description")
    environment: Environment = Field(default=Environment.DEVELOPMENT, description="Runtime environment")

    # Component configurations
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    cors: CORSConfig = Field(default_factory=CORSConfig)
    redis: Optional[RedisConfig] = Field(default=None, description="Redis configuration")

    # Feature flags
    enable_metrics: bool = Field(default=True, description="Enable Prometheus metrics")
    enable_tracing: bool = Field(default=False, description="Enable distributed tracing")
    enable_rate_limiting: bool = Field(default=True, description="Enable rate limiting")

    @field_validator("version", mode='before')
    @classmethod
    def validate_version(cls, v: str | Version) -> Version:
        if isinstance(v, Version):
            return v
        try:
            return Version(v)
        except InvalidVersion as e:
            raise ValueError(f"Invalid version format '{v}': {e}") from e

    @field_serializer("version")
    def serialize_version(self, v: Version) -> str:
        return str(v)

    @field_validator("environment")
    @classmethod
    def validate_environment_settings(cls, v: Environment, info) -> Environment:
        if hasattr(info.data, 'server'):
            if v == Environment.PRODUCTION and info.data.server.debug:
                logger.warning("Debug mode enabled in production environment")
            if v == Environment.DEVELOPMENT:
                info.data.server.reload = True
        return v

    @property
    def is_development(self) -> bool:
        return self.environment == Environment.DEVELOPMENT

    @property
    def is_production(self) -> bool:
        return self.environment == Environment.PRODUCTION
