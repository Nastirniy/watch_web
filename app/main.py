import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.backend.config import settings
from app.backend.utils.logging_config import setup_logging, get_logger
from app.backend.database import init_db
from app.backend.middleware.auth import SecurityMiddleware
from app.backend.routes import auth, users, observations, posts, health
from app.backend.enums.routes import AuthRoutes, UserRoutes, ObservationRoutes, PostRoutes, SystemRoutes

# Initialize logging system
setup_logging(
    log_file_path=settings.logging.file_path if settings.logging.enable_file_logging else None,
    log_level=settings.logging.level,
    environment=settings.app.environment
)

# Get application logger
logger = get_logger("main")


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

# Add CORS middleware (must be before other middleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.security.allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"],
    allow_headers=["*"],
    max_age=600
)

# Add security middleware
app.add_middleware(SecurityMiddleware)

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