"""
Database models initialization.
Imports all models to ensure they are registered with SQLModel.
"""

from backend.database.models.base import (
    BaseModel,
    BaseModelWithSoftDelete,
    SoftDeleteMixin,
    StatusMixin,
    TimestampMixin,
)

from backend.database.models.user import (
    Permission,
    Role,
    RolePermission,
    User,
    UserRole,
    UserSession,
)

from backend.database.models.post import (
    ConnectionStatus,
    Post,
)

from backend.database.models.signal import (
    Category,
    ConfidenceLevel,
    ModulationType,
    Observation,
    ObservationStatus,
    SignalType,
)

from backend.database.models.media import (
    FileChunk,
    MediaFile,
    MediaType,
)

from backend.database.models.export import (
    ExportJob,
    JobStatus,
    JobType,
)

from backend.database.models.audit import (
    AuditAction,
    AuditLog,
    AuditSeverity,
    ResourceType,
)

# Export all models for easy importing
__all__ = [
    # Base models
    "BaseModel",
    "BaseModelWithSoftDelete",
    "SoftDeleteMixin",
    "StatusMixin",
    "TimestampMixin",

    # User models
    "Permission",
    "Role",
    "RolePermission",
    "User",
    "UserRole",
    "UserSession",

    # Post models
    "ConnectionStatus",
    "Post",

    # Signal models
    "Category",
    "ConfidenceLevel",
    "ModulationType",
    "Observation",
    "ObservationStatus",
    "SignalType",

    # Media models
    "FileChunk",
    "MediaFile",
    "MediaType",

    # Export models
    "ExportJob",
    "JobStatus",
    "JobType",

    # Audit models
    "AuditAction",
    "AuditLog",
    "AuditSeverity",
    "ResourceType",
]
