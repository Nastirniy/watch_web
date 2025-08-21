from typing import Optional

from pydantic import BaseModel, Field


class RedisConfig(BaseModel):
    """Redis configuration for caching and sessions."""
    host: str = Field(default="localhost", description="Redis host")
    port: int = Field(default=6379, ge=1, le=65535, description="Redis port")
    db: int = Field(default=0, ge=0, le=15, description="Redis database number")
    password: Optional[str] = Field(default=None, description="Redis password (omit if no auth)")
    decode_responses: bool = Field(default=True, description="Decode responses to strings")

    # Connection pool settings
    max_connections: int = Field(default=10, ge=1, description="Max connections in pool")
    retry_on_timeout: bool = Field(default=True, description="Retry on timeout")

    @property
    def url(self) -> str:
        """Generate Redis URL."""
        auth = f":{self.password}@" if self.password else ""
        return f"redis://{auth}{self.host}:{self.port}/{self.db}"
