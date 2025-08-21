"""
API routes initialization.
Exports API version module for inclusion in the main application.
"""

from backend.api import v1

# Export all routers
__all__ = [
    "v1",
]
