from enum import Enum


class Permissions(Enum):
    """Permission names as stored in the permissions table.

    X.archive hides a record (is_deleted=true) and restores it; nothing is ever removed.
    role.delete is the single permanent delete: it removes an archived role row and
    its assignments for good. Human-readable names live in PERMISSION_META (database.py).
    """
    # User permissions
    USER_VIEW = "user.view"
    USER_CREATE = "user.create"
    USER_UPDATE = "user.update"
    USER_ARCHIVE = "user.archive"

    # Post permissions
    POST_VIEW = "post.view"
    POST_CREATE = "post.create"
    POST_UPDATE = "post.update"
    POST_ARCHIVE = "post.archive"

    # Observation permissions
    OBSERVATION_VIEW = "observation.view"
    OBSERVATION_CREATE = "observation.create"
    OBSERVATION_UPDATE = "observation.update"
    OBSERVATION_ARCHIVE = "observation.archive"

    # Role permissions
    ROLE_VIEW = "role.view"
    ROLE_CREATE = "role.create"
    ROLE_UPDATE = "role.update"
    ROLE_ARCHIVE = "role.archive"
    ROLE_DELETE = "role.delete"  # permanent removal, the only hard delete in the API

    # Admin permissions
    IS_ADMIN = "admin.true"
    AUDIT_VIEW = "audit.view"
    EXPORT_CREATE = "export.create"

    # Category permissions
    CATEGORY_VIEW = "category.view"
    CATEGORY_CREATE = "category.create"
    CATEGORY_UPDATE = "category.update"
    CATEGORY_ARCHIVE = "category.archive"

    # SignalType permissions
    SIGNAL_TYPE_VIEW = "signal_type.view"
    SIGNAL_TYPE_CREATE = "signal_type.create"
    SIGNAL_TYPE_UPDATE = "signal_type.update"
    SIGNAL_TYPE_ARCHIVE = "signal_type.archive"

    # Tag permissions
    TAG_VIEW = "tag.view"
    TAG_CREATE = "tag.create"
    TAG_UPDATE = "tag.update"
    TAG_ARCHIVE = "tag.archive"

    # Media permissions
    MEDIA_VIEW = "media.view"
    MEDIA_CREATE = "media.create"
    MEDIA_UPDATE = "media.update"
    MEDIA_ARCHIVE = "media.archive"

    # Data scope and ownership permissions
    POST_ACCESS_ALL = "post.access_all"
    OBSERVATION_MANAGE_ANY = "observation.manage_any"
    MEDIA_MANAGE_ANY = "media.manage_any"

    # Accepted upload formats (administration dictionary)
    MEDIA_FORMAT_VIEW = "media_format.view"
    MEDIA_FORMAT_CREATE = "media_format.create"
    MEDIA_FORMAT_UPDATE = "media_format.update"
    MEDIA_FORMAT_ARCHIVE = "media_format.archive"

    # Administration dashboard
    STATISTICS_VIEW = "statistics.view"

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


class FrequencyKind(Enum):
    """Kind of a frequency band of an observation (observation_frequencies.kind).
    A new kind is a new value here; the database column has no CHECK on it."""
    RC = "rc"
    VIDEO = "video"


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


class ThumbnailSize(str, Enum):
    """Bounding boxes rendered on demand by imgproxy (see ThumbnailSettings)"""
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"
    PREVIEW = "preview"


class ThumbnailFormat(str, Enum):
    """Output format; AUTO lets imgproxy pick WebP when the client accepts it"""
    AUTO = "auto"
    JPEG = "jpeg"
    WEBP = "webp"


class StatsGroupBy(str, Enum):
    """Bucket size of time series statistics"""
    HOUR = "hour"
    DAY = "day"


class PosterStatus(str, Enum):
    """Lifecycle of the poster frame extracted from a video by the media worker"""
    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class ThumbnailStatus(str, Enum):
    """What a client can expect from GET /media/{id}/thumbnail"""
    READY = "ready"
    PENDING = "pending"
    PROCESSING = "processing"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"


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
