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

    # Admin permissions
    ADMIN_FULL = "admin.full"
    AUDIT_VIEW = "audit.view"
    EXPORT_CREATE = "export.create"


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
