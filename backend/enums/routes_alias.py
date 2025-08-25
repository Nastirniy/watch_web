from enum import Enum


class DocsRoutes(str, Enum):
    """FastAPI documentation routes"""
    BASE = ""  # No base prefix for docs
    SWAGGER_UI = "/docs"
    REDOC = "/redoc"

    def full_path(self) -> str:
        """Get the full path with base prefix"""
        return f"{self.BASE}{self}" if self.BASE else str(self)


class AuthRoutes(str, Enum):
    """Authentication and authorization routes"""
    BASE = "/auth"
    LOGIN = "/login"
    REFRESH = "/refresh"
    LOGOUT = "/logout"
    CHANGE_PASSWORD = "/change-password"
    ME = "/me"
    SESSIONS = "/sessions"
    CHECK_PERMISSION = "/check-permission"
    CERTIFICATE_VALIDATE = "/certificate/validate"

    def full_path(self) -> str:
        """Get the full path with base prefix"""
        return f"{self.BASE}{self}" if self.BASE else str(self)


class ExportRoutes(str, Enum):
    """Export job management routes"""
    BASE = "/exports"
    JOB_BY_ID = "/{job_id}"
    JOB_ACTIONS = "/{job_id}/actions"
    JOB_DOWNLOAD = "/{job_id}/download"
    JOB_PROGRESS = "/{job_id}/progress"
    BULK = "/bulk"
    QUEUE_STATUS = "/queue/status"
    STATISTICS = "/statistics"
    POLICY = "/policy"
    MAINTENANCE_CLEANUP = "/maintenance/cleanup"

    def full_path(self) -> str:
        """Get the full path with base prefix"""
        return f"{self.BASE}{self}" if self.BASE else str(self)


class MediaRoutes(str, Enum):
    """Media file management routes"""
    BASE = "/media"
    UPLOAD = "/upload"
    UPLOAD_CHUNKED_START = "/upload/chunked/start"
    UPLOAD_CHUNKED_CHUNK = "/upload/chunked/chunk"
    MEDIA_BY_ID = "/{media_id}"
    MEDIA_DOWNLOAD = "/{media_id}/download"
    MEDIA_INFO = "/{media_id}/info"
    API_V1_DOWNLOAD = "/api/v1/media/{media_id}/download"  # This one has its own prefix
    BULK = "/bulk"
    STATISTICS = "/statistics"
    MAINTENANCE_CLEANUP = "/maintenance/cleanup"
    POLICY = "/policy"

    def full_path(self) -> str:
        """Get the full path with base prefix"""
        # Special case for API_V1_DOWNLOAD which already has full path
        if self == self.API_V1_DOWNLOAD:
            return str(self)
        return f"{self.BASE}{self}" if self.BASE else str(self)


class PostRoutes(str, Enum):
    """Post/content management routes"""
    BASE = "/posts"
    ACTIVE = "/active"
    POST_BY_ID = "/{post_id}"
    POST_STATUS = "/{post_id}/status"
    BULK_STATUS_UPDATE = "/bulk/status-update"
    SEARCH_LOCATION = "/search/location"
    STATISTICS = "/statistics"
    BY_STATUS = "/by-status/{status}"
    POST_ACTIVITY = "/{post_id}/activity"
    REGIONS_SUMMARY = "/regions/summary"
    MAINTENANCE_CLEANUP = "/maintenance/cleanup-offline"

    def full_path(self) -> str:
        """Get the full path with base prefix"""
        return f"{self.BASE}{self}" if self.BASE else str(self)


class SignalRoutes(str, Enum):
    """Signal observation management routes"""
    BASE = "/signals"
    TYPES = "/types"
    TYPE_BY_ID = "/types/{signal_type_id}"
    OBSERVATIONS = "/observations"
    OBSERVATION_BY_ID = "/observations/{observation_id}"
    OBSERVATIONS_BULK_UPDATE = "/observations/bulk-update"
    OBSERVATIONS_STATISTICS = "/observations/statistics"
    OBSERVATION_MARK_DISAPPEARED = "/observations/{observation_id}/mark-disappeared"

    def full_path(self) -> str:
        """Get the full path with base prefix"""
        return f"{self.BASE}{self}" if self.BASE else str(self)


class UserRoutes(str, Enum):
    """User management routes"""
    BASE = "/users"
    USER_BY_ID = "/{user_id}"
    USER_ROLES = "/{user_id}/roles"
    USER_ROLE_BY_ID = "/{user_id}/roles/{role_id}"
    BULK_ASSIGN_ROLES = "/bulk/assign-roles"
    STATISTICS = "/statistics"

    def full_path(self) -> str:
        """Get the full path with base prefix"""
        return f"{self.BASE}{self}" if self.BASE else str(self)


class APIVersionRoutes(str, Enum):
    """API versioning routes"""
    BASE = ""  # No additional base for API version
    V1 = "/api/v1"

    def full_path(self) -> str:
        """Get the full path with base prefix"""
        return f"{self.BASE}{self}" if self.BASE else str(self)


class SystemRoutes(str, Enum):
    """System and utility routes"""
    BASE = ""  # No base prefix for system routes
    ROOT = "/"
    HEALTH = "/health"
    OPENAPI_JSON = "/openapi.json"
    FAVICON = "/favicon.ico"

    def full_path(self) -> str:
        """Get the full path with base prefix"""
        return f"{self.BASE}{self}" if self.BASE else str(self)


# Helper class to access all routes
class Routes:
    """Centralized access to all route enums"""
    DOCS = DocsRoutes
    AUTH = AuthRoutes
    EXPORTS = ExportRoutes
    MEDIA = MediaRoutes
    POSTS = PostRoutes
    SIGNALS = SignalRoutes
    USERS = UserRoutes
    API_VERSION = APIVersionRoutes
    SYSTEM = SystemRoutes
