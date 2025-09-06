import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from loguru import logger

from app.backend.config import settings
from app.backend.database import init_db
from app.backend.middleware.auth import AuthMiddleware
from app.backend.routes import auth, users, observations, posts, health
from app.backend.enums.routes import AuthRoutes, UserRoutes, ObservationRoutes, PostRoutes, SystemRoutes


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
app.include_router(health.router, tags=[SystemRoutes.tag()])
app.include_router(auth.router, prefix=AuthRoutes.BASE.versioned_path(), tags=[AuthRoutes.tag()])
app.include_router(users.router, prefix=UserRoutes.BASE.versioned_path(), tags=[UserRoutes.tag()])
app.include_router(observations.router, prefix=ObservationRoutes.BASE.versioned_path(), tags=[ObservationRoutes.tag()])
app.include_router(posts.router, prefix=PostRoutes.BASE.versioned_path(), tags=[PostRoutes.tag()])


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=settings.server.host,
        port=settings.server.port,
        reload=settings.server.reload,
        workers=settings.server.workers
    )