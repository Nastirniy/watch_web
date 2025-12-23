from enum import Enum


class APIVersionRoutes(str, Enum):
    """API versioning routes"""
    BASE = ""  # No additional base for API version
    V1 = "/api/v1"

    @classmethod
    def current_version(cls) -> str:
        return str(cls.V1.value)  # TODO: need to be followed as app config

    def path(self) -> str:
        """Get the full path with base prefix"""
        if self == self.BASE:
            return f"{self.BASE.value}"
        return f"{self.BASE.value}{self.value}" if self.BASE.value else str(self.value)

    @staticmethod
    def tag() -> str:
        return "API_VERSIONS"


class DocsRoutes(str, Enum):
    """FastAPI documentation routes"""
    BASE = ""  # No base prefix for docs
    SWAGGER_UI = "/docs"
    REDOC = "/redoc"

    def versioned_path(self) -> str:
        """Get the full path with base prefix and api version prefix"""
        api_ver = APIVersionRoutes.current_version()
        if self == self.BASE:
            return f"{api_ver}{self.BASE.value}"
        return f"{api_ver}{self.BASE.value}{self.value}" if self.BASE.value else f"{api_ver}{self.value}"

    def path(self) -> str:
        """Get the full path with base prefix"""
        if self == self.BASE:
            return f"{self.BASE.value}"
        return f"{self.BASE.value}{self.value}" if self.BASE.value else str(self.value)

    @staticmethod
    def tag() -> str:
        return "DOCS"


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

    def versioned_path(self) -> str:
        """Get the full path with base prefix and api version prefix"""
        api_ver = APIVersionRoutes.current_version()
        if self == self.BASE:
            return f"{api_ver}{self.BASE.value}"
        return f"{api_ver}{self.BASE.value}{self.value}" if self.BASE.value else f"{api_ver}{self.value}"

    def path(self) -> str:
        """Get the full path with base prefix"""
        if self == self.BASE:
            return f"{self.BASE.value}"
        return f"{self.BASE.value}{self.value}" if self.BASE.value else str(self.value)

    @staticmethod
    def tag() -> str:
        return "AUTHENTICATION"


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

    def versioned_path(self) -> str:
        """Get the full path with base prefix and api version prefix"""
        api_ver = APIVersionRoutes.current_version()
        if self == self.BASE:
            return f"{api_ver}{self.BASE.value}"
        return f"{api_ver}{self.BASE.value}{self.value}" if self.BASE.value else f"{api_ver}{self.value}"

    def path(self) -> str:
        """Get the full path with base prefix"""
        if self == self.BASE:
            return f"{self.BASE.value}"
        return f"{self.BASE.value}{self.value}" if self.BASE.value else str(self.value)

    @staticmethod
    def tag() -> str:
        return "EXPORTS"


class PostRoutes(str, Enum):
    """Post/content management routes"""
    BASE = "/posts"
    POST_BY_ID = "/{post_id}"
    POST_STATUS = "/{post_id}/status"
    BULK_STATUS_UPDATE = "/bulk/status-update"
    SEARCH_LOCATION = "/search/location"
    STATISTICS = "/statistics"
    BY_STATUS = "/by-status/{status}"
    POST_ACTIVITY = "/{post_id}/activity"
    REGIONS_SUMMARY = "/regions/summary"
    MAINTENANCE_CLEANUP = "/maintenance/cleanup-offline"

    def versioned_path(self) -> str:
        """Get the full path with base prefix and api version prefix"""
        api_ver = APIVersionRoutes.current_version()
        if self == self.BASE:
            return f"{api_ver}{self.BASE.value}"
        return f"{api_ver}{self.BASE.value}{self.value}" if self.BASE.value else f"{api_ver}{self.value}"

    def path(self) -> str:
        """Get the full path with base prefix"""
        if self == self.BASE:
            return f"{self.BASE.value}"
        return f"{self.BASE.value}{self.value}" if self.BASE.value else str(self.value)

    @staticmethod
    def tag() -> str:
        return "POSTS"


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

    def versioned_path(self) -> str:
        """Get the full path with base prefix and api version prefix"""
        api_ver = APIVersionRoutes.current_version()
        if self == self.BASE:
            return f"{api_ver}{self.BASE.value}"
        return f"{api_ver}{self.BASE.value}{self.value}" if self.BASE.value else f"{api_ver}{self.value}"

    def path(self) -> str:
        """Get the full path with base prefix"""
        if self == self.BASE:
            return f"{self.BASE.value}"
        return f"{self.BASE.value}{self.value}" if self.BASE.value else str(self.value)

    @staticmethod
    def tag() -> str:
        return "SIGNALS"


class ObservationRoutes(str, Enum):
    """Observation management routes"""
    BASE = "/observations"
    CREATE = "/create"
    LIST_ALL = "/list-all"
    OBSERVATION_BY_ID = "/{observation_id}"
    MARK_DISAPPEARED = "/{observation_id}/disappeared"
    BULK_UPDATE = "/bulk-update"
    STATISTICS = "/statistics"

    def versioned_path(self) -> str:
        """Get the full path with base prefix and api version prefix"""
        api_ver = APIVersionRoutes.current_version()
        if self == self.BASE:
            return f"{api_ver}{self.BASE.value}"
        return f"{api_ver}{self.BASE.value}{self.value}" if self.BASE.value else f"{api_ver}{self.value}"

    def path(self) -> str:
        """Get the full path with base prefix"""
        if self == self.BASE:
            return f"{self.BASE.value}"
        return f"{self.BASE.value}{self.value}" if self.BASE.value else str(self.value)

    @staticmethod
    def tag() -> str:
        return "OBSERVATIONS"


class MediaRoutes(str, Enum):
    """Media file management routes"""
    BASE = "/media"
    UPLOAD = "/upload"
    # UPLOAD_CHUNKED_START = "/upload/chunked/start"
    # UPLOAD_CHUNKED_CHUNK = "/upload/chunked/chunk"
    MEDIA_BY_ID = "/{media_id}"
    MEDIA_DOWNLOAD = "/{media_id}/download"
    MEDIA_INFO = "/{media_id}/info"
    MEDIA_THUMBNAIL = "/{media_id}/thumbnail"
    MEDIA_THUMBNAIL_REGENERATE = "/{media_id}/thumbnail/regenerate"
    LIST_ALL_ATTACHED_MEDIA = "/list-all-attached"
    # OBSERVATION_THUMBNAILS = "/observation/{observation_id}/thumbnails"
    STORAGE_STATISTICS = "/statistics"
    # MAINTENANCE_CLEANUP = "/maintenance/cleanup"
    # CHECK_UPLOAD_SESSION = "/check-upload-session"
    # RESUME_UPLOAD = "/resume-upload"
    # FILE_INTEGRITY = "/integrity/{media_id}"
    # FILE_REPAIR = "/repair/{media_id}"
    # CLEANUP_OLD_CHUNKS = "/cleanup/old-chunks"

    def versioned_path(self) -> str:
        """Get the full path with base prefix and api version prefix"""
        api_ver = APIVersionRoutes.current_version()
        if self == self.BASE:
            return f"{api_ver}{self.BASE.value}"
        return f"{api_ver}{self.BASE.value}{self.value}" if self.BASE.value else f"{api_ver}{self.value}"

    def path(self) -> str:
        """Get the full path with base prefix"""
        if self == self.BASE:
            return f"{self.BASE.value}"
        return f"{self.BASE.value}{self.value}" if self.BASE.value else str(self.value)

    @staticmethod
    def tag() -> str:
        return "MEDIA"


class UserRoutes(str, Enum):
    """User management routes"""
    BASE = "/users"
    USER_BY_ID = "/{user_id}"
    USER_ROLES = "/{user_id}/roles"

    def versioned_path(self) -> str:
        """Get the full path with base prefix and api version prefix"""
        api_ver = APIVersionRoutes.current_version()
        if self == self.BASE:
            return f"{api_ver}{self.BASE.value}"
        return f"{api_ver}{self.BASE.value}{self.value}" if self.BASE.value else f"{api_ver}{self.value}"

    def path(self) -> str:
        """Get the full path with base prefix"""
        if self == self.BASE:
            return f"{self.BASE.value}"
        return f"{self.BASE.value}{self.value}" if self.BASE.value else str(self.value)

    @staticmethod
    def tag() -> str:
        return "USERS"


class RoleRoutes(str, Enum):
    """Role management routes"""
    BASE = "/roles"
    ROLE_BY_ID = "/{role_id}"
    ROLE_PERMISSIONS = "/{role_id}/permissions"
    ROLE_PERMISSION_BY_ID = "/{role_id}/permissions/{permission_id}"
    ALL_PERMISSIONS = "/list-all-permissions"

    def versioned_path(self) -> str:
        """Get the full path with base prefix and api version prefix"""
        api_ver = APIVersionRoutes.current_version()
        if self == self.BASE:
            return f"{api_ver}{self.BASE.value}"
        return f"{api_ver}{self.BASE.value}{self.value}" if self.BASE.value else f"{api_ver}{self.value}"

    def path(self) -> str:
        """Get the full path with base prefix"""
        if self == self.BASE:
            return f"{self.BASE.value}"
        return f"{self.BASE.value}{self.value}" if self.BASE.value else str(self.value)

    @staticmethod
    def tag() -> str:
        return "ROLES"


class CategoryRoutes(str, Enum):
    """Category management routes"""
    BASE = "/categories"
    LIST_ALL = "/list-all"
    CREATE = "/create"
    CATEGORY_BY_ID = "/{category_id}"
    CATEGORY_SIGNAL_TYPES = "/{category_id}/signal-types"

    def versioned_path(self) -> str:
        """Get the full path with base prefix and api version prefix"""
        api_ver = APIVersionRoutes.current_version()
        if self == self.BASE:
            return f"{api_ver}{self.BASE.value}"
        return f"{api_ver}{self.BASE.value}{self.value}" if self.BASE.value else f"{api_ver}{self.value}"

    def path(self) -> str:
        """Get the full path with base prefix"""
        if self == self.BASE:
            return f"{self.BASE.value}"
        return f"{self.BASE.value}{self.value}" if self.BASE.value else str(self.value)

    @staticmethod
    def tag() -> str:
        return "CATEGORIES"


class SignalTypeRoutes(str, Enum):
    """Signal type management routes"""
    BASE = "/signal-types"
    LIST_ALL = "/list-all"
    CREATE = "/create"
    SIGNAL_TYPE_BY_ID = "/{signal_type_id}"

    def versioned_path(self) -> str:
        """Get the full path with base prefix and api version prefix"""
        api_ver = APIVersionRoutes.current_version()
        if self == self.BASE:
            return f"{api_ver}{self.BASE.value}"
        return f"{api_ver}{self.BASE.value}{self.value}" if self.BASE.value else f"{api_ver}{self.value}"

    def path(self) -> str:
        """Get the full path with base prefix"""
        if self == self.BASE:
            return f"{self.BASE.value}"
        return f"{self.BASE.value}{self.value}" if self.BASE.value else str(self.value)

    @staticmethod
    def tag() -> str:
        return "SIGNAL_TYPES"


class SystemRoutes(str, Enum):
    """System and utility routes"""
    BASE = ""  # No base prefix for system routes
    ROOT = "/"
    HEALTH = "/health"
    OPENAPI_JSON = "/openapi.json"
    STATIC = "/static/"
    FAVICON = "/favicon.ico"

    def versioned_path(self) -> str:
        """Get the full path with base prefix and api version prefix"""
        api_ver = APIVersionRoutes.current_version()
        if self == self.BASE:
            return f"{api_ver}{self.BASE.value}"
        return f"{api_ver}{self.BASE.value}{self.value}" if self.BASE.value else f"{api_ver}{self.value}"

    def path(self) -> str:
        """Get the full path with base prefix"""
        if self == self.BASE:
            return f"{self.BASE.value}"
        return f"{self.BASE.value}{self.value}" if self.BASE.value else str(self.value)

    @staticmethod
    def tag() -> str:
        return "SYSTEM"


# Helper class to access all routes
class Routes:
    """Centralized access to all route enums"""
    DOCS = DocsRoutes
    AUTH = AuthRoutes
    EXPORTS = ExportRoutes
    POSTS = PostRoutes
    SIGNALS = SignalRoutes
    OBSERVATIONS = ObservationRoutes
    MEDIA = MediaRoutes
    USERS = UserRoutes
    ROLES = RoleRoutes
    CATEGORIES = CategoryRoutes
    SIGNAL_TYPES = SignalTypeRoutes
    API_VERSION = APIVersionRoutes
    SYSTEM = SystemRoutes


if __name__ == "__main__":
    # Test route enum functionality
    from app.backend.utils.logging_config import get_logger
    logger = get_logger("routes")
    logger.debug(f"Auth routes test: {Routes.AUTH.BASE.versioned_path()}")