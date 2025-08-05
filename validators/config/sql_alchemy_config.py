from loguru import logger
from pydantic import BaseModel, ConfigDict, Field


class DatabaseConfig(BaseModel):
    """SQLAlchemy database configuration."""
    model_config = ConfigDict(str_strip_whitespace=True)

    # Connection settings
    driver: str = Field(default="postgresql+asyncpg", description="SQLAlchemy driver")
    host: str = Field(default="localhost", description="Database host")
    port: int = Field(default=5432, ge=1, le=65535, description="Database port")
    name: str = Field(..., min_length=1, description="Database name")
    username: str = Field(..., min_length=1, description="Database username")
    password: str = Field(..., min_length=1, description="Database password")

    # Connection pool settings
    pool_size: int = Field(default=10, ge=1, description="Connection pool size")
    max_overflow: int = Field(default=20, ge=0, description="Max overflow connections")
    pool_timeout: float = Field(default=30.0, ge=0, description="Pool timeout in seconds")
    pool_recycle: int = Field(default=3600, ge=0, description="Pool recycle time in seconds")

    # SQLAlchemy settings
    echo: bool = Field(default=False, description="Enable SQL query logging")
    echo_pool: bool = Field(default=False, description="Enable connection pool logging")

    @property
    def url(self) -> str:
        """Generate SQLAlchemy database URL."""
        return f"{self.driver}://{self.username}:{self.password}@{self.host}:{self.port}/{self.name}"

    @property
    def async_url(self) -> str:
        """Generate async SQLAlchemy database URL."""
        if not self.driver.startswith(("postgresql+asyncpg", "mysql+aiomysql", "sqlite+aiosqlite")):
            logger.warning(f"Driver '{self.driver}' may not support async operations")
        return self.url
