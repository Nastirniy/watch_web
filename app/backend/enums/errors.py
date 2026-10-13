"""Problem types of the API (RFC 9457 Problem Details) and field error codes of a 422.

Every error answers Content-Type application/problem+json with
    type      URI of the problem type: <base>/problems/<code>; dereferencing it gives its documentation
    title     fixed summary of the problem type (never changes between occurrences)
    status    HTTP status code of the problem type
    detail    explanation of this occurrence (the title with the params filled in)
    instance  path of the request that failed
    code      the last segment of type: the key of exactly this problem in the web localization package
    <params>  extension members typed below (post_id, offset, ...)

A code names exactly one message, never a category: a new message is a new code. Every problem type
defines its status, title and params here (RFC 9457 section 4), so /problems/<code>, the OpenAPI schemas
and the checks in tests/test_error_contract.py all come from this file.

Raise with ApiError(ErrorCode.X, **params) (errors/problems.py), access_denied() for 403, and
invalid(FieldErrorCode.X, **params) inside request model validators (items of a validation_error);
services raise CodedError(ErrorCode.X, **params) (errors/coded.py).
"""
import string
from enum import Enum
from typing import Any, Dict, List, Literal, NamedTuple, Optional

from pydantic import BaseModel, Field

from app.backend.enums.general import UserType


class RequiredPermissions(BaseModel):
    """Permission requirement of a route, mirrors auth.require.Require"""
    all: List[str] = Field(default_factory=list, description="Every permission in this list is required")
    any: List[str] = Field(default_factory=list, description="At least one permission in this list is required")


class UserRef(BaseModel):
    id: int
    username: str


class ExcessRole(BaseModel):
    id: int
    name: str
    display_name: str


class ExcessScope(BaseModel):
    """The node of an assignment: the root, a scope group or a post"""
    kind: Literal["root", "group", "post"]
    id: Optional[int] = Field(description="Id of the group or the post; null at the root")
    name: str = Field(description="Name of the group or the post; the application name at the root")


class ExcessAssignment(BaseModel):
    """An assignment whose role holds permissions the ceiling of the user's type does not allow"""
    id: int
    role: ExcessRole
    scope: ExcessScope
    excess_permissions: List[str]


class ExcessHolder(BaseModel):
    """A user holding a role whose added permissions the ceiling of the user's type does not allow"""
    id: int
    username: str
    user_type: UserType
    excess_permissions: List[str]


class ProblemSpec(NamedTuple):
    status: int
    title: str
    detail: str
    params: Dict[str, Any]


class FieldSpec(NamedTuple):
    detail: str
    params: Dict[str, Any]


def problem(status: int, title: str, detail: Optional[str] = None, **params: Any) -> ProblemSpec:
    """status and title of a problem type; detail is a template over params (defaults to the title).
    A param typed Optional[...] may be left out; every other param is required at every raise."""
    return ProblemSpec(status, title, detail or title, params)


def field(detail: str, **params: Any) -> FieldSpec:
    return FieldSpec(detail, params)


def _text(value: Any) -> Any:
    return ", ".join(map(str, value)) if isinstance(value, (list, tuple)) else value


def placeholders(template: str) -> set:
    return {name for _, name, _, _ in string.Formatter().parse(template) if name}


class ProblemType(str, Enum):
    """str Enum of problem types: X = ("code", problem(status, title, detail, **params))"""

    def __new__(cls, value: str, spec: ProblemSpec):
        member = str.__new__(cls, value)
        member._value_ = value
        member.status = spec.status
        member.title = spec.title
        member.detail_template = spec.detail
        member.params = spec.params
        return member

    def render(self, params: dict) -> str:
        """detail of an occurrence; lists read as "a, b" """
        return self.detail_template.format(**{k: _text(v) for k, v in params.items()})


class FieldCode(str, Enum):
    """Base of field error codes (items of a validation_error): X = ("code", field(detail, **params))"""

    def __new__(cls, value: str, spec: FieldSpec):
        member = str.__new__(cls, value)
        member._value_ = value
        member.detail_template = spec.detail
        member.params = spec.params
        return member

    def render(self, params: dict) -> str:
        return self.detail_template.format(**{k: _text(v) for k, v in params.items()})


# 403 bodies always say what the route requires and what the caller lacks
_ACCESS = dict(required=Optional[RequiredPermissions], missing=Optional[List[str]])
# 415 bodies always say what the endpoint accepts
_FORMATS = dict(media_type=str, accepted_content_types=List[str], accepted_extensions=List[str])


class AuthErrorCode(ProblemType):
    """401: the frontend tells these apart to decide between a new login and a token refresh."""
    CREDENTIALS_REQUIRED = ("credentials_required", problem(401, "Authentication credentials required"))
    # Also a refresh token sent as bearer: the client contract treats both as "get a new access token"
    INVALID_TOKEN = ("invalid_token", problem(401, "Invalid authentication token"))
    INVALID_REFRESH_TOKEN = ("invalid_refresh_token", problem(401, "Invalid refresh token"))
    CERTIFICATE_VERIFICATION_FAILED = ("certificate_verification_failed", problem(401, "Certificate verification failed"))
    CERTIFICATE_DATA_MISSING = ("certificate_data_missing", problem(401, "Invalid certificate data"))
    CERTIFICATE_NOT_RECOGNIZED = ("certificate_not_recognized", problem(401, "Certificate not recognized"))
    # The certificate is registered but revoked: the person asks for a new one (an expired one is not recognized)
    CERTIFICATE_REVOKED = ("certificate_revoked", problem(401, "Certificate revoked"))
    SESSION_EXPIRED = ("session_expired", problem(401, "Session expired or invalidated"))
    # The session was opened with another client certificate than the one this request presents (RFC 8705)
    TOKEN_CERTIFICATE_MISMATCH = ("token_certificate_mismatch", problem(401, "Token belongs to another client certificate"))
    ACCOUNT_NOT_FOUND = ("account_not_found", problem(401, "Account not found"))
    ACCOUNT_DEACTIVATED = ("account_deactivated", problem(401, "Account deactivated"))
    ACCOUNT_DELETED = ("account_deleted", problem(401, "Account deleted"))


class AccessErrorCode(ProblemType):
    """403: authorization failures, with the required / missing permissions."""
    PERMISSION_DENIED = ("permission_denied", problem(403, "Insufficient permissions", **_ACCESS))
    PERMISSION_REQUIRED_FOR_ACTION = ("permission_required_for_action", problem(
        403, "Permission required for this action", "Permission {permission} is required for {action}",
        permission=str, action=str, **_ACCESS))
    POST_ACCESS_DENIED = ("post_access_denied", problem(
        403, "No access to the data of this post", "Access denied: you can only view {resource_type} data of your assigned posts",
        resource_type=str, post_id=int, **_ACCESS))
    POST_WRITE_DENIED = ("post_write_denied", problem(
        403, "Read-only access to this post", "Access denied: you have read-only access to post {post_id}",
        post_id=int, resource_type=str, **_ACCESS))
    NO_POST_ASSIGNED = ("no_post_assigned", problem(403, "You are not assigned to any post. Please contact an administrator.", **_ACCESS))
    OBSERVATION_NOT_OWNER = ("observation_not_owner", problem(403, "You can only modify your own observations", **_ACCESS))
    MEDIA_NOT_OWNER = ("media_not_owner", problem(403, "You can only archive or restore media files you uploaded", **_ACCESS))
    UPLOAD_SESSION_NOT_OWNER = ("upload_session_not_owner", problem(403, "You can only use upload sessions you opened", **_ACCESS))
    USER_SELF_ONLY = ("user_self_only", problem(403, "You can only view your own account or users of your assigned posts", **_ACCESS))
    # A role assigned at a node (missing: its permissions the caller does not hold there), permissions added to or
    # removed from a role, a role archived or restored (missing: what the caller does not hold anywhere)
    GRANT_EXCEEDS_OWN_PERMISSIONS = ("grant_exceeds_own_permissions", problem(
        403, "You can only grant or revoke permissions you hold yourself", **_ACCESS))
    # Fields, home post, sessions or archive state of a user the caller's user.* permission does not cover, or who
    # has an assignment the caller could not grant (missing)
    TARGET_USER_MORE_PRIVILEGED = ("target_user_more_privileged", problem(
        403, "This user holds permissions you do not hold", **_ACCESS))
    OBSERVATION_NOT_ACTIVE = ("observation_not_active", problem(
        403, "Observation is no longer active",
        "Cannot archive or restore media of an observation that is no longer active (status: {observation_status})",
        observation_status=str, **_ACCESS))
    HEALTH_HOST_NOT_ALLOWED = ("health_host_not_allowed", problem(403, "This host is not allowed to access the health endpoint", **_ACCESS))


class ErrorCode(ProblemType):
    """Every other problem type: 400, 404, 409, 410, 413, 415, 422, 429, 5xx."""

    # ---------------------------------------------------------------- request and framework
    VALIDATION_ERROR = ("validation_error", problem(
        422, "Request validation failed", "The request has {error_count} invalid value(s)",
        error_count=int, errors=List[Dict[str, Any]]))
    ROUTE_NOT_FOUND = ("route_not_found", problem(404, "Route not found"))
    # No params: the page answers for any path segment, which is never echoed back
    PROBLEM_TYPE_NOT_FOUND = ("problem_type_not_found", problem(404, "Problem type not found"))
    METHOD_NOT_ALLOWED = ("method_not_allowed", problem(405, "Method not allowed"))
    BODY_PARSE_ERROR = ("body_parse_error", problem(400, "The request body could not be parsed"))
    INTERNAL_ERROR = ("internal_error", problem(
        500, "Internal server error", "Internal server error; quote {error_id} when reporting it", error_id=str))
    DATA_TOO_LONG = ("data_too_long", problem(422, "Data too long for one or more fields"))
    FORM_CONTENT_TYPE_REQUIRED = ("form_content_type_required", problem(
        415, "Form fields required",
        "Send the fields as multipart/form-data or application/x-www-form-urlencoded, not {content_type}", content_type=str))
    HEALTH_RATE_LIMITED = ("health_rate_limited", problem(
        429, "Rate limit exceeded", "Rate limit exceeded: at most {max_requests} requests per {window_seconds} seconds",
        max_requests=int, window_seconds=int))

    # ---------------------------------------------------------------- auth and sessions
    LOGIN_DATA_REQUIRED = ("login_data_required", problem(400, "JSON authentication data required in development mode"))
    SESSION_NOT_FOUND = ("session_not_found", problem(404, "Session not found"))
    SESSION_ALREADY_ENDED = ("session_already_ended", problem(409, "Session already ended", end_reason=Optional[str]))
    SESSION_ALREADY_INVALIDATED = ("session_already_invalidated", problem(400, "Session already invalidated"))

    # ---------------------------------------------------------------- users
    USER_NOT_FOUND = ("user_not_found", problem(404, "User not found"))
    USER_NOT_ARCHIVED = ("user_not_archived", problem(409, "User is not archived"))
    USERNAME_TAKEN = ("username_taken", problem(400, "User with this username already exists"))
    USERNAME_OR_CERTIFICATE_TAKEN = ("username_or_certificate_taken", problem(400, "User with this username or certificate already exists"))
    CERTIFICATE_TAKEN = ("certificate_taken", problem(400, "Certificate already registered"))
    CERTIFICATE_FILE_EMPTY = ("certificate_file_empty", problem(400, "Certificate file is empty"))
    CERTIFICATE_FILE_TOO_LARGE = ("certificate_file_too_large", problem(
        413, "Certificate file is too large", "Certificate file is too large (at most {max_bytes} bytes)", max_bytes=int))
    CERTIFICATE_INVALID = ("certificate_invalid", problem(400, "Invalid certificate"))
    CERTIFICATE_AUTHORITY_NOT_CONFIGURED = ("certificate_authority_not_configured", problem(
        503, "Certificate authority not configured"))
    # The signing service (pki-signer) did not answer: nothing was issued, the request may be repeated
    CERTIFICATE_AUTHORITY_UNAVAILABLE = ("certificate_authority_unavailable", problem(
        503, "Certificate authority unavailable"))
    # No such certificate for the user of the path (also the id of another user's certificate)
    CERTIFICATE_NOT_FOUND = ("certificate_not_found", problem(404, "Certificate not found"))
    # Export of a revoked or expired certificate
    CERTIFICATE_NOT_ACTIVE = ("certificate_not_active", problem(409, "Certificate is not active"))
    # Export of a certificate the API did not issue (uploaded, legacy, configured): there is no package
    CERTIFICATE_NOT_EXPORTABLE = ("certificate_not_exportable", problem(409, "Certificate has no stored package"))
    # The folder of an issued certificate was removed by hand
    CERTIFICATE_FILES_MISSING = ("certificate_files_missing", problem(410, "Certificate files missing"))
    CANNOT_DEACTIVATE_SELF = ("cannot_deactivate_self", problem(400, "You cannot deactivate your own account"))
    CANNOT_ARCHIVE_SELF = ("cannot_archive_self", problem(400, "You cannot archive your own account"))

    # ---------------------------------------------------------------- roles and permissions
    ROLE_NOT_FOUND = ("role_not_found", problem(404, "Role not found", role_id=Optional[int]))
    ROLE_NOT_ARCHIVED = ("role_not_archived", problem(409, "Role is not archived"))
    ROLE_NAME_TAKEN = ("role_name_taken", problem(400, "Role with this name already exists"))
    ROLE_NAME_ARCHIVED = ("role_name_archived", problem(
        409, "Role name belongs to an archived role",
        "Role with this name exists as an archived role ({role_id}); restore it or choose another name", role_id=int))
    BUILTIN_ROLE_NOT_ARCHIVABLE = ("builtin_role_not_archivable", problem(409, "Built-in roles cannot be archived"))
    BUILTIN_ROLE_NOT_DELETABLE = ("builtin_role_not_deletable", problem(409, "Built-in roles cannot be deleted"))
    BUILTIN_ROLE_NOT_RENAMABLE = ("builtin_role_not_renamable", problem(409, "Built-in roles cannot be renamed"))
    # Seeding restores the permissions of every built-in role at start; the API never changes their set (clone it)
    BUILTIN_ROLE_PERMISSIONS_FIXED = ("builtin_role_permissions_fixed", problem(
        409, "The permissions of built-in roles cannot be changed"))
    LAST_ADMIN_REQUIRED = ("last_admin_required", problem(409, "This change would leave no active administrator"))
    CANNOT_REMOVE_OWN_ADMIN = ("cannot_remove_own_admin", problem(409, "You cannot remove administrator access from yourself"))
    ROLE_DELETE_REQUIRES_ARCHIVE = ("role_delete_requires_archive", problem(409, "Role must be archived before it can be deleted permanently"))
    ROLE_HAS_ACTIVE_USERS = ("role_has_active_users", problem(
        409, "Role is still assigned to active users; unassign it before deleting permanently", assigned_users=List[UserRef]))
    ROLE_PERMISSION_NOT_ASSIGNED = ("role_permission_not_assigned", problem(404, "Permission is not assigned to this role"))
    PERMISSION_NOT_FOUND = ("permission_not_found", problem(404, "Permission not found"))

    # ---------------------------------------------------------------- role assignments
    ASSIGNMENT_NOT_FOUND = ("assignment_not_found", problem(404, "Role assignment not found"))
    ASSIGNMENT_EXISTS = ("assignment_exists", problem(409, "The user already holds this role at this scope"))
    # Every user keeps at least one assignment (spec section 6 rule 6)
    LAST_ASSIGNMENT_REQUIRED = ("last_assignment_required", problem(
        409, "The last role assignment of a user cannot be removed"))
    # A role holding a system permission is assignable only at the root (spec section 3)
    ROLE_REQUIRES_ROOT_SCOPE = ("role_requires_root_scope", problem(
        409, "This role can be assigned only at the root",
        "The role holds system permissions ({system_permissions}): it can be assigned only at the root",
        system_permissions=List[str]))
    # Every role a user holds fits the ceiling of the user's type (spec section 4)
    ROLE_EXCEEDS_USER_TYPE = ("role_exceeds_user_type", problem(
        409, "The role holds permissions the user's type does not allow",
        "A user of type {user_type} cannot hold: {excess_permissions}",
        user_type=UserType, excess_permissions=List[str]))
    # A type change keeps every other assignment: each must fit the ceiling of the new type (spec section 4)
    USER_TYPE_CEILING_EXCEEDED = ("user_type_ceiling_exceeded", problem(
        409, "Assignments of the user exceed the ceiling of the new type",
        "A user of type {user_type} cannot hold the roles of the listed assignments: remove them first",
        user_type=UserType, assignments=List[ExcessAssignment]))
    # Permissions added to a role reach every holder of the role: each holder's type must allow them
    ROLE_HOLDERS_EXCEED_USER_TYPE = ("role_holders_exceed_user_type", problem(
        409, "The type of a user holding this role does not allow the added permissions",
        users=List[ExcessHolder]))

    # ---------------------------------------------------------------- posts
    POST_NOT_FOUND = ("post_not_found", problem(404, "Post not found", post_id=Optional[int]))
    POST_CODE_TAKEN = ("post_code_taken", problem(400, "Post with this code already exists"))
    POST_NOT_ARCHIVED = ("post_not_archived", problem(409, "Post is not archived"))
    # The data of an archived post is read-only history; it cannot become a home post either
    POST_ARCHIVED = ("post_archived", problem(
        409, "Post is archived", "Post {post_id} is archived: its data is read-only", post_id=int))

    # ---------------------------------------------------------------- scope groups
    SCOPE_GROUP_NOT_FOUND = ("scope_group_not_found", problem(404, "Scope group not found"))
    SCOPE_GROUP_NOT_ARCHIVED = ("scope_group_not_archived", problem(409, "Scope group is not archived"))
    SCOPE_GROUP_NOT_EMPTY = ("scope_group_not_empty", problem(
        409, "Scope group is not empty", "Scope group has active groups or posts: move or archive them first"))
    SCOPE_GROUP_CYCLE = ("scope_group_cycle", problem(
        409, "Scope group cannot move into its own subtree", "A group cannot become a child of itself or of its descendant"))
    SCOPE_GROUP_DEPTH_EXCEEDED = ("scope_group_depth_exceeded", problem(
        409, "Scope group tree is too deep", "Groups can be nested at most {max_depth} levels deep", max_depth=int))
    SCOPE_GROUP_NAME_TAKEN = ("scope_group_name_taken", problem(
        409, "Scope group name already exists", "A group with this name already exists under the same parent"))

    # ---------------------------------------------------------------- dictionaries
    CATEGORY_NOT_FOUND = ("category_not_found", problem(404, "Category not found", category_id=Optional[int]))
    CATEGORY_NOT_ARCHIVED = ("category_not_archived", problem(409, "Category is not archived"))
    CATEGORY_NAME_TAKEN = ("category_name_taken", problem(
        400, "Category name already exists", "Category with name '{name}' already exists", name=str))
    CATEGORY_HAS_SIGNAL_TYPES = ("category_has_signal_types", problem(
        409, "Cannot archive a category that has signal types"))
    CATEGORY_NAME_ARCHIVED = ("category_name_archived", problem(
        400, "Category name belongs to an archived category", "Category with name '{name}' already exists as an archived category", name=str))
    TAG_NOT_FOUND = ("tag_not_found", problem(404, "Tag not found"))
    TAGS_NOT_FOUND = ("tags_not_found", problem(404, "Tags not found", "Tags not found: {tag_ids}", tag_ids=List[int]))
    TAG_NOT_ARCHIVED = ("tag_not_archived", problem(409, "Tag is not archived"))
    TAG_NAME_TAKEN = ("tag_name_taken", problem(400, "Tag name already exists", "Tag with name '{name}' already exists", name=str))
    TAG_NAME_ARCHIVED = ("tag_name_archived", problem(
        400, "Tag name belongs to an archived tag", "Tag with name '{name}' already exists as an archived tag", name=str))
    SIGNAL_TYPE_NOT_FOUND = ("signal_type_not_found", problem(404, "Signal type not found", signal_type_id=Optional[int]))
    SIGNAL_TYPE_NOT_ARCHIVED = ("signal_type_not_archived", problem(409, "Signal type is not archived"))
    SIGNAL_TYPE_CODE_TAKEN = ("signal_type_code_taken", problem(
        400, "Signal type code already exists", "Signal type with code '{signal_type_code}' already exists", signal_type_code=str))
    SIGNAL_TYPE_CODE_ARCHIVED = ("signal_type_code_archived", problem(
        400, "Signal type code belongs to an archived signal type",
        "Signal type with code '{signal_type_code}' already exists as an archived signal type", signal_type_code=str))
    SIGNAL_TYPE_INACTIVE = ("signal_type_inactive", problem(
        400, "Signal type is inactive", "Signal type '{signal_type_name}' is inactive and cannot be used", signal_type_name=str))
    SIGNAL_TYPE_CATEGORY_ARCHIVED = ("signal_type_category_archived", problem(
        409, "The category of the signal type is archived"))
    SIGNAL_TYPE_HAS_OBSERVATIONS = ("signal_type_has_observations", problem(400, "Cannot archive a signal type that has observations"))

    # ---------------------------------------------------------------- observations and frequencies
    OBSERVATION_NOT_FOUND = ("observation_not_found", problem(404, "Observation not found"))
    OBSERVATION_SIGNAL_TYPE_ARCHIVED = ("observation_signal_type_archived", problem(
        409, "The signal type of the observation is archived"))
    OBSERVATION_NOT_ARCHIVED = ("observation_not_archived", problem(409, "Observation is not archived"))
    FREQUENCY_NOT_FOUND = ("frequency_not_found", problem(404, "Frequency not found"))
    FREQUENCY_NOT_ARCHIVED = ("frequency_not_archived", problem(409, "Frequency is not archived"))
    LAST_FREQUENCY_NOT_ARCHIVABLE = ("last_frequency_not_archivable", problem(409, "The last active frequency of an observation cannot be archived"))

    # ---------------------------------------------------------------- statistics
    PERIOD_REVERSED = ("period_reversed", problem(
        400, "Period start is after its end", "{fields}: start must not be after end", fields=str))
    PERIOD_TOO_LONG = ("period_too_long", problem(
        400, "Period too long", "Period longer than {max_days} days for group_by={group_by}", max_days=int, group_by=str))
    UNKNOWN_TIME_ZONE = ("unknown_time_zone", problem(400, "Unknown time zone", "Unknown time zone: {time_zone}", time_zone=str))

    # ---------------------------------------------------------------- media files
    MEDIA_NOT_FOUND = ("media_not_found", problem(404, "Media file not found"))
    MEDIA_NOT_ARCHIVED = ("media_not_archived", problem(409, "Media file is not archived"))
    MEDIA_OBSERVATION_NOT_FOUND = ("media_observation_not_found", problem(404, "Associated observation not found"))
    MEDIA_FILE_MISSING = ("media_file_missing", problem(404, "File is missing on disk"))
    RANGE_NOT_SATISFIABLE = ("range_not_satisfiable", problem(
        416, "Range not satisfiable", "No byte of the requested range exists in the file of {size_bytes} bytes",
        size_bytes=int))
    MEDIA_PATH_UNRESOLVED = ("media_path_unresolved", problem(500, "Cannot resolve file path relative to storage root"))
    MEDIA_TYPE_UNKNOWN = ("media_type_unknown", problem(415, "Unknown media type", "Unknown media type '{media_type}'", **_FORMATS))
    MEDIA_EXTENSION_NOT_ACCEPTED = ("media_extension_not_accepted", problem(
        415, "File extension not accepted", "File extension '{extension}' is not accepted for {media_type}",
        extension=str, **_FORMATS))
    MEDIA_EXTENSION_DANGEROUS = ("media_extension_dangerous", problem(
        415, "File extension not allowed", "File extension '{extension}' is not allowed", extension=str, **_FORMATS))
    MEDIA_CONTENT_TYPE_NOT_ACCEPTED = ("media_content_type_not_accepted", problem(
        415, "Content type not accepted", "Content type '{content_type}' is not accepted for {media_type}",
        content_type=str, **_FORMATS))
    MEDIA_CONTENT_TYPE_EXTENSION_MISMATCH = ("media_content_type_extension_mismatch", problem(
        415, "Content-Type does not match the file extension",
        "Content-Type '{content_type}' does not match the file extension (expected {expected_content_type})",
        content_type=str, expected_content_type=str, **_FORMATS))
    MEDIA_FILE_TOO_LARGE = ("media_file_too_large", problem(
        413, "File too large for its format", "File exceeds the {max_file_size_bytes} byte limit for {content_type}",
        content_type=str, max_file_size_bytes=int, file_size_bytes=Optional[int]))
    MEDIA_FILE_EMPTY = ("media_file_empty", problem(400, "File is empty"))
    MEDIA_CONTENT_INVALID = ("media_content_invalid", problem(400, "File content does not match its type"))
    MEDIA_UPLOADS_BUSY = ("media_uploads_busy", problem(503, "Too many concurrent uploads, try again later"))
    MEDIA_UPLOAD_SOURCE_MISSING = ("media_upload_source_missing", problem(400, "Uploaded file not found"))
    MEDIA_STORE_FAILED = ("media_store_failed", problem(500, "Failed to store the file"))
    NGINX_UPLOAD_DISABLED = ("nginx_upload_disabled", problem(501, "Nginx upload offloading is not enabled"))
    NGINX_FILE_NAME_MISSING = ("nginx_file_name_missing", problem(400, "Missing X-File-Name header"))
    NGINX_FILE_NAME_INVALID = ("nginx_file_name_invalid", problem(400, "X-File-Name is not a file in the upload temp directory"))
    THUMBNAILS_UNSUPPORTED = ("thumbnails_unsupported", problem(422, "Thumbnails are not supported for this media type"))
    IMAGE_TOO_LARGE_FOR_THUMBNAILS = ("image_too_large_for_thumbnails", problem(
        422, "Image too large for thumbnails",
        "The image has {megapixels} megapixels; thumbnails are rendered for images of at most {max_megapixels}",
        megapixels=float, max_megapixels=float))
    NO_VIDEO_STREAM = ("no_video_stream", problem(422, "The file has no video stream"))
    POSTER_FAILED = ("poster_failed", problem(
        409, "Poster extraction failed; a holder of media.update can regenerate it", thumbnail_status=str, attempts=int))
    STORYBOARD_FAILED = ("storyboard_failed", problem(409, "Storyboard generation failed", attempts=int))
    STORYBOARD_UNSUPPORTED = ("storyboard_unsupported", problem(
        422, "No storyboard for this media",
        "Storyboards exist for videos of at least {min_duration_seconds} s while they are turned on",
        min_duration_seconds=int))
    THUMBNAIL_RENDERER_ERROR = ("thumbnail_renderer_error", problem(502, "Thumbnail renderer returned an error"))
    THUMBNAIL_RENDERER_UNAVAILABLE = ("thumbnail_renderer_unavailable", problem(503, "Thumbnail renderer unavailable"))

    # ---------------------------------------------------------------- media formats (administration)
    MEDIA_FORMAT_NOT_FOUND = ("media_format_not_found", problem(404, "Media format not found"))
    MEDIA_FORMAT_NOT_ARCHIVED = ("media_format_not_archived", problem(409, "Media format is not archived"))
    MEDIA_FORMAT_EXISTS = ("media_format_exists", problem(
        400, "Media format already exists", "Media format '{content_type}' already exists ({format_id})",
        content_type=str, format_id=int))
    MEDIA_FORMAT_NO_FIELDS = ("media_format_no_fields", problem(400, "No fields to update"))
    MEDIA_FORMAT_SIZE_ABOVE_CEILING = ("media_format_size_above_ceiling", problem(
        400, "Size limit above the server ceiling",
        "max_file_size_bytes {max_file_size_bytes} exceeds the server ceiling of {ceiling_bytes} bytes",
        max_file_size_bytes=int, ceiling_bytes=int))
    MEDIA_FORMAT_CONTENT_TYPE_MISMATCH = ("media_format_content_type_mismatch", problem(
        400, "content_type does not belong to media_type",
        "content_type '{content_type}' must start with '{media_type}/' (put other spellings into aliases)",
        content_type=str, media_type=str))
    MEDIA_FORMAT_EXTENSION_CLASH = ("media_format_extension_clash", problem(
        400, "Extension belongs to another format",
        "Extension(s) {extensions} already belong to format '{format_content_type}' ({format_id})",
        extensions=List[str], format_content_type=str, format_id=int))
    MEDIA_FORMAT_CONTENT_TYPE_CLASH = ("media_format_content_type_clash", problem(
        400, "Content-Type belongs to another format",
        "Content-Type(s) {content_types} already belong to format '{format_content_type}' ({format_id})",
        content_types=List[str], format_content_type=str, format_id=int))

    # ---------------------------------------------------------------- resumable uploads
    UPLOAD_SESSION_NOT_FOUND = ("upload_session_not_found", problem(404, "Upload session not found"))
    UPLOAD_SESSION_CLOSED = ("upload_session_closed", problem(
        410, "Upload session is closed", "Upload session is {session_status}; open a new session",
        session_status=str, media_file_id=Optional[int]))
    UPLOAD_SESSION_COMPLETED = ("upload_session_completed", problem(409, "Session is completed; archive the media file instead"))
    # Another complete is storing the file (status completing): no chunk, abort or second complete meanwhile
    UPLOAD_SESSION_COMPLETING = ("upload_session_completing", problem(409, "Session is being completed; ask GET for its status"))
    UPLOAD_OFFSET_REQUIRED = ("upload_offset_required", problem(400, "Upload-Offset header is required and must be a non-negative integer"))
    UPLOAD_OFFSET_MISMATCH = ("upload_offset_mismatch", problem(
        409, "Upload-Offset does not match the session offset",
        "Upload-Offset {sent_offset} does not match the session offset {offset}; continue from the offset in the Upload-Offset header",
        offset=int, sent_offset=int))
    UPLOAD_CHUNK_CONTENT_TYPE = ("upload_chunk_content_type", problem(415, "Send chunks as application/offset+octet-stream"))
    UPLOAD_CHUNK_EMPTY = ("upload_chunk_empty", problem(400, "Empty chunk"))
    UPLOAD_CHUNK_TOO_LARGE = ("upload_chunk_too_large", problem(
        413, "Chunk too large", "Chunk exceeds max_chunk_bytes ({max_chunk_bytes})", max_chunk_bytes=int, offset=int))
    UPLOAD_CHUNK_OVERRUN = ("upload_chunk_overrun", problem(
        413, "Chunk overruns the declared expected_size", expected_size=int, offset=int))
    UPLOAD_WRITER_BUSY = ("upload_writer_busy", problem(
        409, "Another chunk is being written to this session; retry after it finishes", offset=int))
    UPLOAD_CHUNK_NOT_STORED = ("upload_chunk_not_stored", problem(
        400, "Chunk was not stored; ask GET for the current offset and resend", offset=int))
    UPLOAD_INCOMPLETE = ("upload_incomplete", problem(
        409, "Upload incomplete", "{missing_bytes} byte(s) still missing; continue sending chunks from offset {offset}",
        missing_bytes=int, offset=int, expected_size=int))
    UPLOAD_BYTES_MISSING = ("upload_bytes_missing", problem(410, "Stored bytes are missing; open a new session", session_status=str))
    UPLOAD_CHECKSUM_MISMATCH = ("upload_checksum_mismatch", problem(
        422, "sha256 of the received bytes does not match; the session is aborted, open a new one",
        expected_sha256=str, actual_sha256=str))


class FieldErrorCode(FieldCode):
    """Rules of the API on single values. Built-in checks keep pydantic's codes (missing, string_too_short, ...)."""
    VALUE_EMPTY = ("value_empty", field("Value cannot be empty"))
    VALUE_NULL = ("value_null", field("Value cannot be null, omit the field to keep the current value"))
    VALUE_NOT_STRING = ("value_not_string", field("Input must be a string"))
    STRING_TOO_LONG = ("string_too_long", field("String should have at most {max_length} characters", max_length=int))
    TEXT_SQL_PATTERN = ("text_sql_pattern", field("Text contains a forbidden SQL keyword"))
    TEXT_SCRIPT_PATTERN = ("text_script_pattern", field("Text contains a forbidden script pattern"))
    USERNAME_INVALID = ("username_invalid", field("Username must be 3-50 characters: letters, digits and . _ -"))
    USERNAME_RESERVED = ("username_reserved", field("Username is reserved"))
    EMAIL_INVALID = ("email_invalid", field("Invalid email format"))
    IP_ADDRESS_INVALID = ("ip_address_invalid", field("Invalid IP address format"))
    FILENAME_INVALID = ("filename_invalid", field("Invalid filename: only letters, digits, dots, dashes and underscores are allowed"))
    FILENAME_TRAVERSAL = ("filename_traversal", field("Path traversal detected in filename"))
    FILENAME_TOO_LONG = ("filename_too_long", field("Filename too long"))
    INTEGER_REQUIRED = ("integer_required", field("Value must be an integer"))
    VALUE_TOO_SMALL = ("value_too_small", field("Value must be at least {min_value}", min_value=float))
    VALUE_TOO_LARGE = ("value_too_large", field("Value must be at most {max_value}", max_value=float))
    COORDINATES_NOT_NUMERIC = ("coordinates_not_numeric", field("Coordinates must be numeric"))
    LATITUDE_OUT_OF_RANGE = ("latitude_out_of_range", field("Latitude must be between -90 and 90"))
    LONGITUDE_OUT_OF_RANGE = ("longitude_out_of_range", field("Longitude must be between -180 and 180"))
    PERMISSION_IDS_DUPLICATE = ("permission_ids_duplicate", field("Duplicate permission IDs are not allowed"))
    PERMISSIONS_UNKNOWN = ("permissions_unknown", field("Unknown permissions: {permissions}", permissions=List[str]))
    SCOPE_ID_REQUIRED = ("scope_id_required", field("A group or post scope needs the id of the group or post"))
    SCOPE_ID_NOT_ALLOWED = ("scope_id_not_allowed", field("The root scope has no id"))
    CERTIFICATE_CN_INVALID = ("certificate_cn_invalid", field("Invalid certificate CN format"))
    CERTIFICATE_SERIAL_INVALID = ("certificate_serial_invalid", field("Invalid certificate serial format"))
    CERTIFICATE_FINGERPRINT_INVALID = ("certificate_fingerprint_invalid", field("Invalid certificate fingerprint format"))
    CERTIFICATE_LABEL_INVALID = ("certificate_label_invalid", field(
        "Certificate label must be 1 to 64 characters without control characters"))
    # The limit is a setting (settings.pki.max_validity_days), so a code of its own instead of pydantic's static le
    VALIDITY_DAYS_TOO_LONG = ("validity_days_too_long", field("Validity is at most {max_days} days", max_days=int))
    SEARCH_LOGIC_INVALID = ("search_logic_invalid", field("logic must be 'and' or 'or'"))
    CONTENT_TYPE_INVALID = ("content_type_invalid", field("Not a valid Content-Type (expected type/subtype, lower case)"))
    EXTENSION_INVALID = ("extension_invalid", field("Not a valid extension (expected .ext, letters and digits, up to 10 characters)"))
    EXTENSIONS_REQUIRED = ("extensions_required", field("At least one extension is required"))
    MEDIA_TYPE_NOT_ACCEPTED = ("media_type_not_accepted", field("media_type must be one of {accepted}", accepted=List[str]))
    CONTENT_TYPE_OUTSIDE_MEDIA_TYPE = ("content_type_outside_media_type", field(
        "content_type must start with '{media_type}/' (put other spellings into aliases)", media_type=str))
    CONFIDENCE_LEVEL_INVALID = ("confidence_level_invalid", field("confidence_level must be one of: {accepted}", accepted=List[str]))
    PAGINATION_NEGATIVE = ("pagination_negative", field("Pagination values must be non-negative"))
    DATE_FILTERS_CONFLICT = ("date_filters_conflict", field(
        "Cannot use both absolute date filters (observed_at_from/to) and relative date filters (last_n_days/day_offset)"))
    RELATIVE_DATE_FILTERS_CONFLICT = ("relative_date_filters_conflict", field("Cannot use both last_n_days and day_offset"))
    OBSERVED_AT_RANGE_REVERSED = ("observed_at_range_reversed", field("observed_at_from must be before or equal to observed_at_to"))
    FREQUENCY_RANGE_INVALID = ("frequency_range_invalid", field("frequency_max must be greater than frequency_min"))
    FREQUENCY_CENTER_OUT_OF_RANGE = ("frequency_center_out_of_range", field("frequency_center must lie between frequency_min and frequency_max"))
    DISAPPEARED_BEFORE_OBSERVED = ("disappeared_before_observed", field("disappeared_at must not be before observed_at"))
    OBSERVED_AFTER_DISAPPEARED = ("observed_after_disappeared", field("observed_at must not be after disappeared_at"))


PROBLEM_ENUMS = (AuthErrorCode, AccessErrorCode, ErrorCode)
ALL_CODE_ENUMS = PROBLEM_ENUMS + (FieldErrorCode,)


def problem_type(code: str) -> Optional[ProblemType]:
    for cls in PROBLEM_ENUMS:
        try:
            return cls(code)
        except ValueError:
            continue
    return None
