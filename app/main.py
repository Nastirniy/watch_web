import asyncio
import sys
import os
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Add project root to Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

from app.backend.config import settings
from app.backend.utils.logging_config import setup_logging, get_logger, get_main_logger
from app.backend.database import initialize_database_with_migrations
from app.backend.routes import auth, users, observations, posts, health, media, roles, categories, signal_types
from app.backend.enums.routes import AuthRoutes, UserRoutes, ObservationRoutes, PostRoutes, SystemRoutes, MediaRoutes, RoleRoutes, CategoryRoutes, SignalTypeRoutes

# Initialize logging system
setup_logging(
    log_file_path=settings.logging.file_path if settings.logging.enable_file_logging else None,
    log_level=settings.logging.level,
    environment=settings.app.environment
)

# Get application logger
_logger = get_main_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle"""
    try:
        _logger.info("Starting application...")
        await initialize_database_with_migrations()
        _logger.info("Database initialization completed. Server ready.")
        yield
        _logger.info("Shutting down application...")
    except Exception as e:
        _logger.error(f"Startup failed: {e}")
        import traceback
        traceback.print_exc()
        raise


fastapi_app = FastAPI(
    title=settings.app.name,
    version=settings.app.version,
    lifespan=lifespan
)

# Add CORS middleware (must be before other middleware)
fastapi_app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.security.allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"],
    allow_headers=["*"],
    max_age=600
)

# Include routers
fastapi_app.include_router(health.router, tags=[SystemRoutes.tag()])
fastapi_app.include_router(auth.router, prefix=AuthRoutes.BASE.versioned_path(), tags=[AuthRoutes.tag()])
fastapi_app.include_router(users.router, prefix=UserRoutes.BASE.versioned_path(), tags=[UserRoutes.tag()])
fastapi_app.include_router(roles.router, prefix=RoleRoutes.BASE.versioned_path(), tags=[RoleRoutes.tag()])
fastapi_app.include_router(categories.router, prefix=CategoryRoutes.BASE.versioned_path(), tags=[CategoryRoutes.tag()])
fastapi_app.include_router(signal_types.router, prefix=SignalTypeRoutes.BASE.versioned_path(), tags=[SignalTypeRoutes.tag()])
fastapi_app.include_router(observations.router, prefix=ObservationRoutes.BASE.versioned_path(), tags=[ObservationRoutes.tag()])
fastapi_app.include_router(posts.router, prefix=PostRoutes.BASE.versioned_path(), tags=[PostRoutes.tag()])
fastapi_app.include_router(media.router, prefix=MediaRoutes.BASE.versioned_path(), tags=[MediaRoutes.tag()])


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "app.main:fastapi_app",
        host=settings.server.host,
        port=settings.server.port,
        reload=settings.server.reload,
        log_level=settings.logging.level.lower()
    )
