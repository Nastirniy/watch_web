from enum import Enum


class Permissions(Enum):
    """Permission names as stored in the permissions table.

    X.archive hides a record (is_deleted=true) and restores it; nothing is ever removed.
    role.delete is the single permanent delete: it removes an archived role row and
    its assignments for good. Human-readable names live in PERMISSION_META (bootstrap/permissions.py).
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

    # Administration (admin.true is gone since authorization v2: administration is these granular permissions)
    AUDIT_VIEW = "audit.view"
    AUDIT_VIEW_SCOPE = "audit.view_scope"  # tree level: the audit log of the node and below, without device fields
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

    # Scope group permissions (nested groups of posts)
    SCOPE_GROUP_VIEW = "scope_group.view"
    SCOPE_GROUP_CREATE = "scope_group.create"
    SCOPE_GROUP_UPDATE = "scope_group.update"
    SCOPE_GROUP_ARCHIVE = "scope_group.archive"

    # Media permissions
    MEDIA_VIEW = "media.view"
    MEDIA_CREATE = "media.create"
    MEDIA_UPDATE = "media.update"
    MEDIA_ARCHIVE = "media.archive"

    # Ownership overrides (post.access_all is gone: every post is an assignment at the root)
    OBSERVATION_MANAGE_ANY = "observation.manage_any"
    MEDIA_MANAGE_ANY = "media.manage_any"

    # Accepted upload formats (administration dictionary)
    MEDIA_FORMAT_VIEW = "media_format.view"
    MEDIA_FORMAT_CREATE = "media_format.create"
    MEDIA_FORMAT_UPDATE = "media_format.update"
    MEDIA_FORMAT_ARCHIVE = "media_format.archive"

    # Administration dashboard
    STATISTICS_VIEW = "statistics.view"

    # Storage and worker monitoring
    MEDIA_MONITOR = "media.monitor"

    # Client certificates of users: issue / upload (create), revoke, and re-download a stored package (export,
    # a system permission)
    CERTIFICATE_VIEW = "certificate.view"
    CERTIFICATE_CREATE = "certificate.create"
    CERTIFICATE_REVOKE = "certificate.revoke"
    CERTIFICATE_EXPORT = "certificate.export"

    # Database and backups (read-only status; system permissions)
    DATABASE_VIEW = "database.view"
    BACKUP_VIEW = "backup.view"


class PermissionLevel(str, Enum):
    """What a permission acts on: tree at a node of the scope tree and below it, system on the whole system (held only
    through system roles)"""
    TREE = "tree"
    SYSTEM = "system"


class RoleKind(str, Enum):
    """A tree role holds only tree permissions and is assigned at a node (role_assignments); a system role holds only
    system permissions and is assigned without a node (system_role_assignments). Never changes."""
    TREE = "tree"
    SYSTEM = "system"


class BuiltinRole(str, Enum):
    """The built-in roles (roles.builtin_key), owned by code (auth/builtin_roles.py) and re-synced at every start"""
    VIEWER = "viewer"
    OPERATOR = "operator"
    SENIOR_OPERATOR = "senior_operator"
    ANALYST = "analyst"
    ADMINISTRATOR = "administrator"
    SYSTEM_ADMINISTRATOR = "system_administrator"
    AUDITOR = "auditor"
    DICTIONARY_EDITOR = "dictionary_editor"
    TAG_EDITOR = "tag_editor"


class UserType(str, Enum):
    """The type of a user, a position: its base roles, its boundary and the pages of the web client
    (auth/user_types.py); never a place. The order is the order of the catalog."""
    VIEWER = "viewer"
    OPERATOR = "operator"
    SENIOR_OPERATOR = "senior_operator"
    ANALYST = "analyst"
    ADMINISTRATOR = "administrator"
    SYSTEM_ADMINISTRATOR = "system_administrator"


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


# Where each kind may sit is ALLOWED_PARENT in services/scope_groups.py; a new level is a new value here plus its entry
# there. The database column (scope_groups.kind) has no CHECK on it.
class ScopeGroupKind(str, Enum):
    """Kind of a scope group: a plain group, or a unit of the organisation (corps > brigade). Fixed at create."""
    GROUP = "group"
    CORPS = "corps"
    BRIGADE = "brigade"


class UnitPlacement(str, Enum):
    """Which unit an observation belongs to for a corps or brigade filter: the one recorded when it was created, or
    the one its post is in now. Not a time filter: both read today's data; the period is its own filter."""
    RECORDED = "recorded"
    CURRENT = "current"


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


class UploadPurpose(str, Enum):
    """What an upload is for; it decides which media types of the format registry apply
    (PURPOSE_MEDIA_TYPES in services/files/formats.py)"""
    OBSERVATION_MEDIA = "observation_media"
    USER_GUIDE = "user_guide"


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
    UNSUPPORTED = "unsupported"     # the file has no video stream (a probe found only audio): never claimed


class StoryboardJobStatus(str, Enum):
    """Lifecycle of a video storyboard job (rows of video_storyboards)"""
    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class StoryboardStatus(str, Enum):
    """What a client can expect from GET /media/{id}/storyboard"""
    READY = "ready"
    PENDING = "pending"
    PROCESSING = "processing"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"


class ThumbnailStatus(str, Enum):
    """What a client can expect from GET /media/{id}/thumbnail"""
    READY = "ready"
    PENDING = "pending"
    PROCESSING = "processing"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"


class SessionStatus(str, Enum):
    """State of a login session, computed when it is read, never stored"""
    ONLINE = "online"  # live, a request within security.online_threshold_seconds
    IDLE = "idle"      # live, the client is quiet; ends after security.session_idle_timeout_minutes
    ENDED = "ended"    # why: SessionEndReason


class SessionEndReason(str, Enum):
    """Why a session ended, from the session's point of view"""
    LOGOUT = "logout"              # the client logged out
    EXPIRED = "expired"            # security.session_max_hours after the login
    IDLE_TIMEOUT = "idle_timeout"  # no request for security.session_idle_timeout_minutes
    REVOKED = "revoked"            # ended by the server before its time; the cause is a SessionRevokeCause


class SessionRevokeCause(str, Enum):
    """What made the server revoke a session; the account itself may stay as it is"""
    ACCOUNT_DEACTIVATED = "account_deactivated"
    ACCOUNT_ARCHIVED = "account_archived"
    # Written before migration 024, when PATCH /users/{id} replaced the user's one certificate; nothing writes it now
    CERTIFICATE_CHANGED = "certificate_changed"
    ENDED_BY_ADMIN = "ended_by_admin"
    USER_TYPE_CHANGED = "user_type_changed"      # the web must not keep showing the pages of the old type
    CERTIFICATE_REVOKED = "certificate_revoked"  # the certificate of the session is no active row of the user
    # The database was restored from a backup (cli/db_backup.py): the sessions of the backup do not come back
    DATABASE_RESTORED = "database_restored"


class ExportJobType(Enum):
    PDF_REPORT = "pdf_report"
    CSV_EXPORT = "csv_export"
    DATABASE_BACKUP = "database_backup"
    STATISTICS_REPORT = "statistics_report"


class BackupLabel(str, Enum):
    """Why a database backup was made (the suffix of its id, services/backups/manifest.py)"""
    SCHEDULED = "scheduled"      # the systemd timer; retention by day, week and month
    MANUAL = "manual"            # an operator; retention by age (backup.keep_manual_days)
    PRE_UPGRADE = "pre-upgrade"  # before an upgrade (DEPLOYMENT.md); retention by age


class ExportJobStatus(Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SortOrder(str, Enum):
    """Sort order options (SQLAlchemy-compatible)"""
    ASCENDING = "ascending"
    DESCENDING = "descending"
    ASCENDING_NULLS_FIRST = "ascending_nulls_first"
    ASCENDING_NULLS_LAST = "ascending_nulls_last"
    DESCENDING_NULLS_FIRST = "descending_nulls_first"
    DESCENDING_NULLS_LAST = "descending_nulls_last"


class ObservationReportType(str, Enum):
    """
    Selects report layout.

    One row per frequency band.
    RC_FREQ_PART    - rc bands only: frequency_min, frequency_max
    VIDEO_FREQ_PART - video bands only: frequency_min, frequency_max
    ALL_FIELDS      - every band with all fields: user, post, kind, emitter group, frequencies, bandwidth, duration, tags
    """
    RC_FREQ_PART    = "rc_freq_part"
    VIDEO_FREQ_PART = "video_freq_part"
    ALL_FIELDS      = "all_fields"

    @property
    def label(self) -> str:
        _labels = {
            "rc_freq_part":    "RC частоти",
            "video_freq_part": "Вiдео частоти",
            "all_fields":      "Повний звiт (всi поля)",
        }
        return _labels[self.value]

    @property
    def description(self) -> str:
        _desc = {
            "rc_freq_part":    "Частота мiн/макс, тип сигналу, коментар",
            "video_freq_part": "Вiдео частота мiн/макс, тип сигналу, коментар",
            "all_fields":      "Всi поля: користувач, пост, тип частоти, група, частоти та смуга, тривалiсть, теги",
        }
        return _desc[self.value]

    @classmethod
    def choices(cls) -> list:
        """Return list of {value, label, description} dicts for web UI select/dropdown."""
        return [
            {"value": m.value, "label": m.label, "description": m.description}
            for m in cls
        ]


class TagMatch(str, Enum):
    """How a list of tags filters observations: at least one of the tags (any) or every one of them (all)"""
    ANY = "any"
    ALL = "all"


class RevocationReason(str, Enum):
    """Why a client certificate was revoked (the RFC 5280 reason written to the CRL)"""
    UNSPECIFIED = "unspecified"
    KEY_COMPROMISE = "key_compromise"
    AFFILIATION_CHANGED = "affiliation_changed"
    SUPERSEDED = "superseded"
    CESSATION_OF_OPERATION = "cessation_of_operation"


class P12Encryption(str, Enum):
    """How a .p12 package is encrypted: AES-256 (modern) or 3DES/SHA-1 (older Windows)"""
    AES256 = "aes256"
    LEGACY = "legacy"


class CertificateSource(str, Enum):
    """Where a row of user_certificates comes from (no DB CHECK: a new source is a new value)"""
    ISSUED = "issued"           # signed by the signer (pki-signer) for the user
    UPLOADED = "uploaded"       # a certificate file from an administrator (POST /users/, init_user)
    LEGACY = "legacy"           # the hashes users held before migration 024; hashes only
    CONFIGURED = "configured"   # the hashes of settings.admin (development seed); hashes only


class CertificateState(str, Enum):
    """State of a certificate, computed on read, never stored: revoked when revoked_at is set, else expired when
    not_after is in the past, else active (a row without not_after stays active until revoked)"""
    ACTIVE = "active"
    EXPIRED = "expired"
    REVOKED = "revoked"
