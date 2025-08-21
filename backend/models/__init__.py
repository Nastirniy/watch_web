"""
Database models initialization.
Imports all models to ensure they are registered with SQLModel.
"""

from backend.models.base import (
    BaseModel,
    BaseModelWithSoftDelete,
    SoftDeleteMixin,
    StatusMixin,
    TimestampMixin,
)

from backend.models.user import (
    Permission,
    Role,
    RolePermission,
    User,
    UserRole,
    UserSession,
)

from backend.models.post import (
    ConnectionStatus,
    Post,
)

from backend.models.signal import (
    Category,
    ConfidenceLevel,
    ModulationType,
    Observation,
    ObservationStatus,
    SignalType,
)

from backend.models.media import (
    FileChunk,
    MediaFile,
    MediaType,
)

from backend.models.export import (
    ExportJob,
    JobStatus,
    JobType,
)

from backend.models.audit import (
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
