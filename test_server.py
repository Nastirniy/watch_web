#!/usr/bin/env python3
"""
Minimal test server to debug the issue
"""
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from contextlib import asynccontextmanager
from fastapi import FastAPI
import uvicorn
from app.backend.config import settings
from app.backend.utils.logging_config import setup_logging, get_logger

# Initialize logging system
setup_logging(
    log_file_path=settings.logging.file_path if settings.logging.enable_file_logging else None,
    log_level=settings.logging.level,
    environment=settings.app.environment
)

logger = get_logger("test")

@asynccontextmanager
async def test_lifespan(app: FastAPI):
    """Simple test lifespan"""
    logger.info("Test application starting...")
    yield
    logger.info("Test application shutting down...")

test_app = FastAPI(
    title="Test API",
    version="1.0.0",
    lifespan=test_lifespan
)

@test_app.get("/")
async def read_root():
    return {"message": "Test server is running!"}

if __name__ == "__main__":
    uvicorn.run(
        "test_server:test_app",
        host="127.0.0.1",
        port=60801,
        reload=False,
        log_level="info"
    )