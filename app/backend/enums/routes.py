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
    ME = "/me"
    SESSIONS = "/sessions"
    CHECK_PERMISSION = "/check-permission"
    ROUTE_ACCESS = "/route-access"

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


class PostRoutes(str, Enum):
    """Post/content management routes"""
    BASE = "/posts"
    FILTER_SCHEMA = "/filter-schema"
    POST_BY_ID = "/{post_id}"

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


class ObservationRoutes(str, Enum):
    """Observation management routes"""
    BASE = "/observations"
    CREATE = "/create"
    LIST_ALL = "/list-all"
    FILTER_SCHEMA = "/filter-schema"
    REPORT = "/report"
    REPORT_TYPES = "/report/types"
    OBSERVATION_BY_ID = "/{observation_id}"
    MARK_DISAPPEARED = "/{observation_id}/disappeared"

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
    FILTER_SCHEMA = "/filter-schema"
    UPLOAD = "/upload"
    NGINX_UPLOAD = "/nginx-upload"
    # UPLOAD_CHUNKED_START = "/upload/chunked/start"
    # UPLOAD_CHUNKED_CHUNK = "/upload/chunked/chunk"
    MEDIA_BY_ID = "/{media_id}"
    MEDIA_STREAM = "/{media_id}/stream"
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
    FILTER_SCHEMA = "/filter-schema"
    USER_BY_ID = "/{user_id}"
    USER_ROLES = "/{user_id}/roles"
    USER_POST_ACCESS = "/{user_id}/post-access"
    USER_POST_ACCESS_BY_POST = "/{user_id}/post-access/{post_id}"

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
    FILTER_SCHEMA = "/filter-schema"
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
    FILTER_SCHEMA = "/filter-schema"
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
    FILTER_SCHEMA = "/filter-schema"
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


class TagRoutes(str, Enum):
    """Tag management routes"""
    BASE = "/tags"
    LIST_ALL = "/list-all"
    CREATE = "/create"
    FILTER_SCHEMA = "/filter-schema"
    TAG_BY_ID = "/{tag_id}"

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
        return "TAGS"


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


class SearchRoutes(str, Enum):
    """Unified search routes"""
    BASE = "/search"
    FILTER_SCHEMA = "/filter-schema"
    OBSERVATIONS = "/observations"
    USERS = "/users"
    POSTS = "/posts"
    ROLES = "/roles"
    CATEGORIES = "/categories"
    SIGNAL_TYPES = "/signal-types"
    TAGS = "/tags"
    MEDIA = "/media"

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
        return "SEARCH"


class StatisticsRoutes(str, Enum):
    """Administration dashboard statistics"""
    BASE = "/statistics"

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
        return "STATISTICS"


# Helper class to access all routes
class Routes:
    """Centralized access to all route enums"""
    DOCS = DocsRoutes
    AUTH = AuthRoutes
    POSTS = PostRoutes
    STATISTICS = StatisticsRoutes
    OBSERVATIONS = ObservationRoutes
    MEDIA = MediaRoutes
    USERS = UserRoutes
    ROLES = RoleRoutes
    CATEGORIES = CategoryRoutes
    SIGNAL_TYPES = SignalTypeRoutes
    TAGS = TagRoutes
    API_VERSION = APIVersionRoutes
    SYSTEM = SystemRoutes
    SEARCH = SearchRoutes


if __name__ == "__main__":
    # Test route enum functionality
    from app.backend.utils.logging_config import get_logger
    logger = get_logger("routes")
    logger.debug(f"Auth routes test: {Routes.AUTH.BASE.versioned_path()}")