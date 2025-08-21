"""
API routes initialization.
Exports all API router modules for inclusion in the main application.
"""

from backend.api.v1 import auth, exports, media, posts, signals, users

# Export all routers
__all__ = [
    "auth",
    "users",
    "posts",
    "signals",
    "media",
    "exports",
]