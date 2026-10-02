"""Every URL of the service, as enums. No route path is written anywhere else.

- APIVersionRoutes: the published API versions; the value is the prefix of the version router (routes/v1.py).
- VersionedRouteEnum groups (AuthRoutes, UserRoutes, ...): BASE is the prefix of the resource router,
  every other member a path relative to it. versioned_path() = version prefix + BASE + member.
- RouteEnum groups (SystemRoutes, DocsRoutes): outside any version, path() only.
"""
from enum import Enum
from typing import Optional


class APIVersionRoutes(str, Enum):
    """Published API versions; the value is the prefix of the version router"""
    V1 = "/api/v1"

    @classmethod
    def current(cls) -> "APIVersionRoutes":
        """The version new clients use; versioned_path() builds it when no version is given"""
        return cls.V1


class RouteEnum(str, Enum):
    """Base of a route group: BASE is the router prefix, every other member a path relative to it"""

    def path(self) -> str:
        """Full path without an API version"""
        base = type(self).BASE
        return base.value if self is base else base.value + self.value


class VersionedRouteEnum(RouteEnum):
    """Route group published under an API version"""

    def versioned_path(self, version: Optional[APIVersionRoutes] = None) -> str:
        """Full path under the given version, the current one by default"""
        return (version or APIVersionRoutes.current()).value + self.path()


class DocsRoutes(RouteEnum):
    """OpenAPI document and its viewers (FastAPI registers them from these values)"""
    BASE = ""
    OPENAPI_JSON = "/openapi.json"
    SWAGGER_UI = "/docs"
    SWAGGER_UI_OAUTH2_REDIRECT = "/docs/oauth2-redirect"
    REDOC = "/redoc"

    @staticmethod
    def tag() -> str:
        return "DOCS"


class SystemRoutes(RouteEnum):
    """System routes outside any API version"""
    BASE = ""
    HEALTH = "/health"
    # RFC 9457 problem type documentation: the "type" URI of every error points here
    PROBLEMS = "/problems"
    PROBLEM_BY_CODE = "/problems/{code}"

    @staticmethod
    def tag() -> str:
        return "SYSTEM"


class AuthRoutes(VersionedRouteEnum):
    """Authentication and authorization routes"""
    BASE = "/auth"
    LOGIN = "/login"
    REFRESH = "/refresh"
    LOGOUT = "/logout"
    ME = "/me"
    SESSIONS = "/sessions"
    CHECK_PERMISSION = "/check-permission"
    ROUTE_ACCESS = "/route-access"

    @staticmethod
    def tag() -> str:
        return "AUTHENTICATION"


class PostRoutes(VersionedRouteEnum):
    """Post/content management routes"""
    BASE = "/posts"
    ROOT = "/"  # collection: list and create (the trailing slash is part of the published path)
    FILTER_SCHEMA = "/filter-schema"
    POST_BY_ID = "/{post_id}"
    POST_OBSERVATION_STATS = "/{post_id}/observation-stats"

    @staticmethod
    def tag() -> str:
        return "POSTS"


class ObservationRoutes(VersionedRouteEnum):
    """Observation management routes"""
    BASE = "/observations"
    CREATE = "/create"
    LIST_ALL = "/list-all"
    FILTER_SCHEMA = "/filter-schema"
    REPORT = "/report"
    REPORT_TYPES = "/report/types"
    OBSERVATION_BY_ID = "/{observation_id}"
    OBSERVATION_ARCHIVE = "/{observation_id}/archive"
    OBSERVATION_RESTORE = "/{observation_id}/restore"
    MARK_DISAPPEARED = "/{observation_id}/disappeared"
    OBSERVATION_FREQUENCIES = "/{observation_id}/frequencies"
    OBSERVATION_FREQUENCY_BY_ID = "/{observation_id}/frequencies/{frequency_id}"
    OBSERVATION_FREQUENCY_ARCHIVE = "/{observation_id}/frequencies/{frequency_id}/archive"
    OBSERVATION_FREQUENCY_RESTORE = "/{observation_id}/frequencies/{frequency_id}/restore"

    @staticmethod
    def tag() -> str:
        return "OBSERVATIONS"


class MediaRoutes(VersionedRouteEnum):
    """Media file management routes"""
    BASE = "/media"
    ROOT = "/"  # collection: list
    FILTER_SCHEMA = "/filter-schema"
    FORMATS = "/formats"
    UPLOAD = "/upload"
    NGINX_UPLOAD = "/nginx-upload"
    # Resumable uploads (see UPLOADS.md)
    UPLOADS = "/uploads"
    UPLOAD_SESSION = "/uploads/{upload_id}"
    UPLOAD_SESSION_COMPLETE = "/uploads/{upload_id}/complete"
    UPLOAD_SESSION_ABORT = "/uploads/{upload_id}/abort"
    MEDIA_ARCHIVE = "/{media_id}/archive"
    MEDIA_RESTORE = "/{media_id}/restore"
    MEDIA_STREAM = "/{media_id}/stream"
    MEDIA_DOWNLOAD = "/{media_id}/download"
    MEDIA_INFO = "/{media_id}/info"
    MEDIA_THUMBNAIL = "/{media_id}/thumbnail"
    MEDIA_THUMBNAIL_REGENERATE = "/{media_id}/thumbnail/regenerate"
    MEDIA_SHARE = "/{media_id}/share"
    POSTERS_STATUS = "/posters/status"
    POSTERS_RECONCILE = "/posters/reconcile"
    LIST_ALL_ATTACHED_MEDIA = "/list-all-attached"
    STORAGE_STATISTICS = "/statistics"

    @staticmethod
    def tag() -> str:
        return "MEDIA"


class UserRoutes(VersionedRouteEnum):
    """User management routes"""
    BASE = "/users"
    ROOT = "/"  # collection: list and create
    FILTER_SCHEMA = "/filter-schema"
    USER_BY_ID = "/{user_id}"
    USER_ARCHIVE = "/{user_id}/archive"
    USER_RESTORE = "/{user_id}/restore"
    USER_ROLES = "/{user_id}/roles"
    USER_POST_ACCESS = "/{user_id}/post-access"
    USER_POST_ACCESS_BY_POST = "/{user_id}/post-access/{post_id}"

    @staticmethod
    def tag() -> str:
        return "USERS"


class RoleRoutes(VersionedRouteEnum):
    """Role management routes"""
    BASE = "/roles"
    ROOT = "/"  # collection: list and create
    FILTER_SCHEMA = "/filter-schema"
    ROLE_BY_ID = "/{role_id}"  # DELETE here is the permanent delete (role.delete)
    ROLE_ARCHIVE = "/{role_id}/archive"
    ROLE_RESTORE = "/{role_id}/restore"
    ROLE_PERMISSIONS = "/{role_id}/permissions"
    ROLE_PERMISSION_BY_ID = "/{role_id}/permissions/{permission_id}"
    ALL_PERMISSIONS = "/list-all-permissions"

    @staticmethod
    def tag() -> str:
        return "ROLES"


class MediaFormatRoutes(VersionedRouteEnum):
    """Accepted upload formats (administration dictionary)"""
    BASE = "/media-formats"
    LIST_ALL = "/list-all"
    CREATE = "/create"
    FILTER_SCHEMA = "/filter-schema"
    FORMAT_BY_ID = "/{format_id}"
    FORMAT_ARCHIVE = "/{format_id}/archive"
    FORMAT_RESTORE = "/{format_id}/restore"

    @staticmethod
    def tag() -> str:
        return "MEDIA_FORMATS"


class CategoryRoutes(VersionedRouteEnum):
    """Category management routes"""
    BASE = "/categories"
    LIST_ALL = "/list-all"
    CREATE = "/create"
    FILTER_SCHEMA = "/filter-schema"
    CATEGORY_BY_ID = "/{category_id}"
    CATEGORY_ARCHIVE = "/{category_id}/archive"
    CATEGORY_RESTORE = "/{category_id}/restore"
    CATEGORY_SIGNAL_TYPES = "/{category_id}/signal-types"

    @staticmethod
    def tag() -> str:
        return "CATEGORIES"


class SignalTypeRoutes(VersionedRouteEnum):
    """Signal type management routes"""
    BASE = "/signal-types"
    LIST_ALL = "/list-all"
    CREATE = "/create"
    FILTER_SCHEMA = "/filter-schema"
    SIGNAL_TYPE_BY_ID = "/{signal_type_id}"
    SIGNAL_TYPE_ARCHIVE = "/{signal_type_id}/archive"
    SIGNAL_TYPE_RESTORE = "/{signal_type_id}/restore"

    @staticmethod
    def tag() -> str:
        return "SIGNAL_TYPES"


class TagRoutes(VersionedRouteEnum):
    """Tag management routes"""
    BASE = "/tags"
    LIST_ALL = "/list-all"
    CREATE = "/create"
    FILTER_SCHEMA = "/filter-schema"
    TAG_BY_ID = "/{tag_id}"
    TAG_ARCHIVE = "/{tag_id}/archive"
    TAG_RESTORE = "/{tag_id}/restore"

    @staticmethod
    def tag() -> str:
        return "TAGS"


class SearchRoutes(VersionedRouteEnum):
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
    MEDIA_FORMATS = "/media-formats"

    @staticmethod
    def tag() -> str:
        return "SEARCH"


class StatisticsRoutes(VersionedRouteEnum):
    """Administration dashboard statistics"""
    BASE = "/statistics"
    ROOT = "/"

    @staticmethod
    def tag() -> str:
        return "STATISTICS"


class Routes:
    """Every route group (tests/test_route_registry.py checks none is missing)"""
    API_VERSION = APIVersionRoutes
    DOCS = DocsRoutes
    SYSTEM = SystemRoutes
    AUTH = AuthRoutes
    POSTS = PostRoutes
    STATISTICS = StatisticsRoutes
    OBSERVATIONS = ObservationRoutes
    MEDIA = MediaRoutes
    MEDIA_FORMATS = MediaFormatRoutes
    USERS = UserRoutes
    ROLES = RoleRoutes
    CATEGORIES = CategoryRoutes
    SIGNAL_TYPES = SignalTypeRoutes
    TAGS = TagRoutes
    SEARCH = SearchRoutes
