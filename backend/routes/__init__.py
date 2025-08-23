# Health check endpoint
from backend.core.config.config import settings
from fastapi_app import app


@app.get("/health", tags=["Health"])
async def health_check() -> dict:
    """Health check endpoint."""
    return {
        "status": "healthy",
        "app_name": settings.APP_NAME,
        "version": settings.APP_VERSION,
    }


# Root endpoint
@app.get("/", tags=["Root"])
async def root() -> dict:
    """Root endpoint with API information."""
    return {
        "message": f"Welcome to {settings.APP_NAME}",
        "version": settings.APP_VERSION,
        "docs_url": "/docs" if settings.DEBUG else None,
    }


# Include API routers
from backend.api.v1 import auth, exports, media, posts, signals, users  # noqa: E402

app.include_router(auth.router, prefix=f"{settings.API_V1_STR}/auth", tags=["Authentication"])
app.include_router(users.router, prefix=f"{settings.API_V1_STR}/users", tags=["Users"])
app.include_router(posts.router, prefix=f"{settings.API_V1_STR}/posts", tags=["Posts"])
app.include_router(signals.router, prefix=f"{settings.API_V1_STR}/signals", tags=["Signals"])
app.include_router(media.router, prefix=f"{settings.API_V1_STR}/media", tags=["Media"])
app.include_router(exports.router, prefix=f"{settings.API_V1_STR}/exports", tags=["Exports"])
