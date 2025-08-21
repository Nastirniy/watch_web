"""
CRUD operations initialization.
Exports all CRUD instances for easy importing.
"""

from backend.database.crud.audit import audit_log
from backend.database.crud.base import CRUDBase
from backend.database.crud.export import export_job
from backend.database.crud.media import file_chunk, media_file
from backend.database.crud.post import post
from backend.database.crud.signal import category, modulation_type, observation, signal_type
from backend.database.crud.user import permission, role, user, user_session

# Export all CRUD instances
__all__ = [
    # Base CRUD class
    "CRUDBase",

    # User-related CRUD
    "user",
    "role",
    "permission",
    "user_session",

    # Post CRUD
    "post",

    # Signal-related CRUD
    "observation",
    "signal_type",
    "category",
    "modulation_type",

    # Media CRUD
    "media_file",
    "file_chunk",

    # Export CRUD
    "export_job",

    # Audit CRUD
    "audit_log",
]
