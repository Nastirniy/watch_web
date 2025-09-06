from enum import Enum


class APIVersionRoutes(str, Enum):
    """API versioning routes"""
    BASE = ""  # No additional base for API version
    V1 = "/api/v1"

    @classmethod
    def version(cls) -> str:
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
        api_ver = APIVersionRoutes.version()
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
        api_ver = APIVersionRoutes.version()
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
        api_ver = APIVersionRoutes.version()
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


class MediaRoutes(str, Enum):
    """Media file management routes"""
    BASE = "/media"
    UPLOAD = "/upload"
    UPLOAD_CHUNKED_START = "/upload/chunked/start"
    UPLOAD_CHUNKED_CHUNK = "/upload/chunked/chunk"
    MEDIA_BY_ID = "/{media_id}"
    MEDIA_DOWNLOAD = "/{media_id}/download"
    MEDIA_INFO = "/{media_id}/info"
    BULK = "/bulk"
    STATISTICS = "/statistics"
    MAINTENANCE_CLEANUP = "/maintenance/cleanup"
    POLICY = "/policy"

    def versioned_path(self) -> str:
        """Get the full path with base prefix and api version prefix"""
        api_ver = APIVersionRoutes.version()
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

    def versioned_path(self) -> str:
        """Get the full path with base prefix and api version prefix"""
        api_ver = APIVersionRoutes.version()
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
        api_ver = APIVersionRoutes.version()
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
    OBSERVATION_BY_ID = "/{observation_id}"
    MARK_DISAPPEARED = "/{observation_id}/disappeared"
    BULK_UPDATE = "/bulk-update"
    STATISTICS = "/statistics"

    def versioned_path(self) -> str:
        """Get the full path with base prefix and api version prefix"""
        api_ver = APIVersionRoutes.version()
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


class UserRoutes(str, Enum):
    """User management routes"""
    BASE = "/users"
    USER_BY_ID = "/{user_id}"
    USER_ROLES = "/{user_id}/roles"
    USER_ROLE_BY_ID = "/{user_id}/roles/{role_id}"
    BULK_ASSIGN_ROLES = "/bulk/assign-roles"
    STATISTICS = "/statistics"

    def versioned_path(self) -> str:
        """Get the full path with base prefix and api version prefix"""
        api_ver = APIVersionRoutes.version()
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
        api_ver = APIVersionRoutes.version()
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
    MEDIA = MediaRoutes
    POSTS = PostRoutes
    SIGNALS = SignalRoutes
    OBSERVATIONS = ObservationRoutes
    USERS = UserRoutes
    API_VERSION = APIVersionRoutes
    SYSTEM = SystemRoutes


if __name__ == "__main__":
    # Test route enum functionality
    from app.backend.utils.logging_config import get_logger
    logger = get_logger("routes")
    logger.debug(f"Auth routes test: {Routes.AUTH.BASE.versioned_path()}")