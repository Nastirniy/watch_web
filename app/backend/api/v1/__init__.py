"""API v1: what /api/v1 publishes.

Each module of this folder carries its own prefix and tag (APIRouter(prefix=XRoutes.BASE.value, ...)), so
the version is only this list. A module of this folder never imports another version's folder; see
"Routes and API versions" in CLAUDE.md.
"""
from app.backend.api.versioning import PublishedVersion
from app.backend.enums.routes import APIVersionRoutes
from app.backend.api.v1 import (
    auth, users, user_certificates, certificates, roles, categories, signal_types, tags, observations, posts,
    scope_groups, corps, brigades, media, media_uploads, media_formats, search, statistics, user_types, database, audit_logs,
    guides, moves, events,
)

# Order matters only inside one prefix: users before user_certificates (both under /users), media before
# media_uploads (both under /media)
V1 = PublishedVersion(
    APIVersionRoutes.V1,
    auth, users, user_certificates, certificates, roles, categories, signal_types, tags, observations, posts,
    scope_groups, corps, brigades, media, media_uploads, media_formats, search, statistics, user_types, database, audit_logs,
    guides, moves, events,
)
