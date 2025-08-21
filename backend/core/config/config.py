"""
Configuration settings for the Signal Monitoring API.
Handles environment variables and application settings.
"""

from functools import lru_cache
from typing import Any, Dict, List, Optional, Union

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore"
    )

    # Application settings
    APP_NAME: str = "Signal Monitoring API"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = False
    API_V1_STR: str = "/api/v1"

    # Server settings
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    WORKERS: int = 1
    RELOAD: bool = False

    # Database settings
    DATABASE_URL: str = Field(
        default="mysql+asyncmy://user:password@localhost:3306/signal_monitoring",
        description="Database connection URL"
    )
    DATABASE_ECHO: bool = False
    DATABASE_POOL_SIZE: int = 10
    DATABASE_MAX_OVERFLOW: int = 20
    DATABASE_POOL_TIMEOUT: int = 30

    # Security settings
    SECRET_KEY: str = Field(
        default="your-super-secret-key-change-this-in-production",
        min_length=32,
        description="Secret key for JWT tokens"
    )
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 30
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    ALGORITHM: str = "HS256"

    # PKI Certificate settings
    PKI_CERT_VALIDATION: bool = True
    PKI_CA_CERT_PATH: Optional[str] = None
    PKI_CRL_PATH: Optional[str] = None

    # File upload settings
    MAX_FILE_SIZE: int = 100 * 1024 * 1024  # 100MB
    ALLOWED_MEDIA_TYPES: List[str] = [
        "image/jpeg", "image/png", "image/gif", "image/webp",
        "audio/mpeg", "audio/wav", "audio/ogg",
        "video/mp4", "video/avi", "video/mov",
        "application/pdf", "text/plain"
    ]
    UPLOAD_PATH: str = "./uploads"
    CHUNK_SIZE: int = 1024 * 1024  # 1MB chunks

    # Redis settings (for caching and sessions)
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_EXPIRE: int = 3600  # 1 hour

    # Logging settings
    LOG_LEVEL: str = "INFO"
    LOG_FILE: Optional[str] = None
    LOG_ROTATION: str = "100 MB"
    LOG_RETENTION: str = "30 days"

    # CORS settings
    BACKEND_CORS_ORIGINS: List[str] = ["http://localhost:3000"]

    # Export settings
    EXPORT_PATH: str = "./exports"
    EXPORT_EXPIRE_HOURS: int = 24
    MAX_EXPORT_RECORDS: int = 100000

    # Rate limiting
    RATE_LIMIT_REQUESTS: int = 100
    RATE_LIMIT_WINDOW: int = 60  # seconds

    @field_validator("BACKEND_CORS_ORIGINS", mode="before")
    @classmethod
    def assemble_cors_origins(cls, v: Union[str, List[str]]) -> Union[List[str], str]:
        """Parse CORS origins from environment variable."""
        if isinstance(v, str) and not v.startswith("["):
            return [i.strip() for i in v.split(",")]
        elif isinstance(v, (list, str)):
            return v
        raise ValueError(v)

    @field_validator("DATABASE_URL", mode="before")
    @classmethod
    def assemble_db_connection(cls, v: Optional[str]) -> str:
        """Validate and assemble database URL."""
        if v is None:
            raise ValueError("DATABASE_URL is required")
        return v

    @property
    def database_url_sync(self) -> str:
        """Get synchronous database URL for migrations."""
        return self.DATABASE_URL.replace("+asyncmy", "")

    @property
    def async_database_url(self) -> str:
        """Get async database URL."""
        if "+asyncmy" not in self.DATABASE_URL:
            return self.DATABASE_URL.replace("mysql://", "mysql+asyncmy://")
        return self.DATABASE_URL


@lru_cache()
def get_settings() -> Settings:
    """Get cached application settings."""
    return Settings()


# Global settings instance
settings = get_settings()
