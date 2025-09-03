import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from loguru import logger

from app.backend.config import settings
from app.backend.database import init_db
from app.backend.middleware.auth import AuthMiddleware
from app.backend.routes import auth, users, observations, posts


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle"""
    logger.info("Starting application...")
    await init_db()
    yield
    logger.info("Shutting down application...")


app = FastAPI(
    title=settings.app.name,
    version=settings.app.version,
    lifespan=lifespan
)

# Add middleware
app.add_middleware(AuthMiddleware)

# Include routers
app.include_router(auth.router, prefix="/api/auth", tags=["Authentication"])
app.include_router(users.router, prefix="/api/users", tags=["Users"])
app.include_router(observations.router, prefix="/api/observations", tags=["Observations"])
app.include_router(posts.router, prefix="/api/posts", tags=["Posts"])


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=settings.server.host,
        port=settings.server.port,
        reload=settings.server.reload,
        workers=settings.server.workers
    )