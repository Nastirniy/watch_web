from typing import Optional, List

from pydantic import BaseModel, Field


class ServerConfig(BaseModel):
    """FastAPI server configuration."""
    host: str = Field(default="0.0.0.0", description="Server bind address")
    port: int = Field(default=60800, ge=1, le=65535, description="Server port")
    workers: int = Field(default=1, ge=1, description="Number of worker processes")
    reload: bool = Field(default=False, description="Enable auto-reload in development")
    debug: bool = Field(default=False, description="Enable debug mode")

    # FastAPI specific settings
    title: str = Field(default="FastAPI Application", description="API title")
    description: Optional[str] = Field(default=None, description="API description")
    docs_url: Optional[str] = Field(default="/docs", description="Swagger UI path")
    redoc_url: Optional[str] = Field(default="/redoc", description="ReDoc path")
    openapi_url: Optional[str] = Field(default="/openapi.json", description="OpenAPI schema path")


class CORSConfig(BaseModel):
    """CORS configuration for FastAPI."""
    allow_origins: List[str] = Field(default=["*"], description="Allowed origins")
    allow_credentials: bool = Field(default=True, description="Allow credentials")
    allow_methods: List[str] = Field(default=["*"], description="Allowed HTTP methods")
    allow_headers: List[str] = Field(default=["*"], description="Allowed headers")
