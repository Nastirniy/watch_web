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

    # Tag permissions
    TAG_VIEW = "tag.view"
    TAG_CREATE = "tag.create"
    TAG_UPDATE = "tag.update"
    TAG_DELETE = "tag.delete"

    # Media permissions
    MEDIA_VIEW = "media.view"
    MEDIA_CREATE = "media.create"
    MEDIA_UPDATE = "media.update"
    MEDIA_DELETE = "media.delete"

    # Data scope and ownership permissions
    POST_ACCESS_ALL = "post.access_all"
    OBSERVATION_MANAGE_ANY = "observation.manage_any"
    MEDIA_MANAGE_ANY = "media.manage_any"

    # Administration dashboard
    STATISTICS_VIEW = "statistics.view"


class AuthErrorCode(str, Enum):
    """Error codes returned in HTTP 401 detail for frontend to distinguish error types."""
    # Token / credentials errors
    CREDENTIALS_REQUIRED = "credentials_required"
    INVALID_TOKEN = "invalid_token"
    INVALID_REFRESH_TOKEN = "invalid_refresh_token"

    # Certificate errors (login only)
    CERTIFICATE_VERIFICATION_FAILED = "certificate_verification_failed"
    CERTIFICATE_DATA_MISSING = "certificate_data_missing"
    CERTIFICATE_NOT_RECOGNIZED = "certificate_not_recognized"

    # Session errors
    SESSION_EXPIRED = "session_expired"
    SESSION_NOT_FOUND = "session_not_found"

    # Account status errors
    ACCOUNT_NOT_FOUND = "account_not_found"
    ACCOUNT_DEACTIVATED = "account_deactivated"
    ACCOUNT_DELETED = "account_deleted"


class AccessErrorCode(str, Enum):
    """Error codes returned in HTTP 403 detail so the frontend can distinguish authorization failures."""
    # Granular permission checks (Require)
    PERMISSION_DENIED = "permission_denied"
    ADMIN_REQUIRED = "admin_required"

    # Post data scope
    POST_ACCESS_DENIED = "post_access_denied"
    POST_WRITE_DENIED = "post_write_denied"
    NO_POST_ASSIGNED = "no_post_assigned"

    # Ownership
    NOT_OWNER = "not_owner"
    USER_SELF_ONLY = "user_self_only"
    OBSERVATION_NOT_ACTIVE = "observation_not_active"


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
