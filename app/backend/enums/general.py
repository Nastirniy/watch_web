from enum import Enum


class Permissions(Enum):
    # User permissions
    USER_VIEW = "user.view"
    USER_CREATE = "user.create"
    USER_UPDATE = "user.update"
    USER_DELETE = "user.delete"

    # Post permissions
    POST_VIEW = "post.view"
    POST_CREATE = "post.create"
    POST_UPDATE = "post.update"
    POST_DELETE = "post.delete"

    # Observation permissions
    OBSERVATION_VIEW = "observation.view"
    OBSERVATION_CREATE = "observation.create"
    OBSERVATION_UPDATE = "observation.update"
    OBSERVATION_DELETE = "observation.delete"

    # Role permissions
    ROLE_VIEW = "role.view"
    ROLE_CREATE = "role.create"
    ROLE_UPDATE = "role.update"
    ROLE_DELETE = "role.delete"

    # Admin permissions
    IS_ADMIN = "admin.true"
    AUDIT_VIEW = "audit.view"
    EXPORT_CREATE = "export.create"

    # Category permissions
    CATEGORY_VIEW = "category.view"
    CATEGORY_CREATE = "category.create"
    CATEGORY_UPDATE = "category.update"
    CATEGORY_DELETE = "category.delete"

    # SignalType permissions
    SIGNAL_TYPE_VIEW = "signal_type.view"
    SIGNAL_TYPE_CREATE = "signal_type.create"
    SIGNAL_TYPE_UPDATE = "signal_type.update"
    SIGNAL_TYPE_DELETE = "signal_type.delete"


class ObservationStatus(Enum):
    ACTIVE = "active"
    DISAPPEARED = "disappeared"
    CONFIRMED = "confirmed"
    FALSE_POSITIVE = "false_positive"
    UNDER_REVIEW = "under_review"


class ConfidenceLevel(Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ConnectionStatus(Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    MAINTENANCE = "maintenance"
    ERROR = "error"


class ServiceHealthStatus(Enum):
    HEALTHY = "healthy"
    UNHEALTHY = "unhealthy"
    DEGRADED = "degraded"
    ERROR = "error"


class ServiceUptimeStatus(Enum):
    RUNNING = "running"
    UNKNOWN = "unknown"


class AppEnvironment(Enum):
    DEVELOPMENT = "development"
    PRODUCTION = "production"


class MediaType(Enum):
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    DOCUMENT = "document"


class ExportJobType(Enum):
    PDF_REPORT = "pdf_report"
    CSV_EXPORT = "csv_export"
    DATABASE_BACKUP = "database_backup"
    STATISTICS_REPORT = "statistics_report"


class ExportJobStatus(Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
