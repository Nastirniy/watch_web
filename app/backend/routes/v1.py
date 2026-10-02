"""API v1: every resource router under /api/v1.

Each resource router carries its own prefix and tag (APIRouter(prefix=XRoutes.BASE.value, ...)),
so a version is only this list. A new version is a module like this one with its own APIRouter;
see "Routes and API versions" in CLAUDE.md.
"""
from fastapi import APIRouter

from app.backend.enums.routes import APIVersionRoutes
from app.backend.routes import (
    auth, users, roles, categories, signal_types, tags, observations, posts, media, media_uploads, media_formats,
    search, statistics,
)

router = APIRouter(prefix=APIVersionRoutes.V1.value)

# Order matters only inside one prefix: media before media_uploads, as both live under /media
for resource in (auth, users, roles, categories, signal_types, tags, observations, posts, media, media_uploads,
                 media_formats, search, statistics):
    router.include_router(resource.router)
