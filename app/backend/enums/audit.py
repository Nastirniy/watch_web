"""Audit events: the catalog of everything the audit log records.

A code is the key of exactly ONE message in the web localization package (the rule of error codes). A member is
X = ("<resource>.<action>", event(AuditCategory.Y, "Title", **params)): the title is the fallback text (the web
localises by code), the params are what the message needs, SNAPSHOTTED when the row is written (a name as it was).
A param typed Optional[...] may be left out; every other param is required at every write.
"""
from enum import Enum
from typing import Any, Dict, FrozenSet, NamedTuple, Optional


class AuditCategory(str, Enum):
    AUTHENTICATION = "authentication"  # login, logout, certificate login attempts
    ACCESS = "access"                  # users, roles, assignments, sessions ended by others, certificates
    STRUCTURE = "structure"            # posts, scope groups, unit history
    DATA = "data"                      # observations, frequencies, media, upload sessions, shares
    DICTIONARIES = "dictionaries"      # categories, signal types, tags, media formats
    READS = "reads"                    # searches (later exports, downloads)
    SYSTEM = "system"                  # poster reconcile, restore reconcile, migrations
    AUDIT = "audit"                    # the audit log's own management; database restore / undo-restore


# Categories that may be switched off; the audit category never (its rows are the record of the log itself)
SWITCHABLE: FrozenSet[AuditCategory] = frozenset(AuditCategory) - {AuditCategory.AUDIT}


class EventSpec(NamedTuple):
    category: AuditCategory
    title: str
    params: Dict[str, Any]


def event(category: AuditCategory, title: str, **params: Any) -> EventSpec:
    """category and title of an event; params are typed (Optional[...] may be left out)"""
    return EventSpec(category, title, params)


_A, _X, _S, _D = AuditCategory.AUTHENTICATION, AuditCategory.ACCESS, AuditCategory.STRUCTURE, AuditCategory.DATA
_DI, _R, _SY, _AU = (AuditCategory.DICTIONARIES, AuditCategory.READS, AuditCategory.SYSTEM, AuditCategory.AUDIT)

_USER = dict(username=str)
_ASSIGN = dict(username=str, role_name=str)
_CERT = dict(username=str, label=str, serial=str, sha256_fingerprint=Optional[str], not_after=Optional[str],
             reason=Optional[str], via=Optional[str])
_ROLE = dict(role_name=str, role_kind=str, cloned_from=Optional[str], permission=Optional[str])
_POST = dict(post_name=str, post_code=str, cascade_from=Optional[int])
_GROUP = dict(group_name=str, group_kind=str, cascade_from=Optional[int])
_OBS = dict(post_name=str, observation_id=int, signal_type_code=str)
_FREQ = dict(observation_id=int, frequency_kind=str)
_MEDIA = dict(observation_id=int, ttl_seconds=Optional[int])
_NAME = dict(name=str)
_SEARCH = dict(result_count=int)
_COUNTS = dict(kept_schema=str, revocations=int, certificates_added=int, users_added=int, crl_number=int,
               sessions_ended=int, ids_raised=int)


class AuditEvent(Enum):
    """Audit events: X = ("code", event(category, title, **params))"""

    def __new__(cls, value: str, spec: EventSpec):
        member = object.__new__(cls)
        member._value_ = value
        member._spec = spec
        return member

    @property
    def code(self) -> str:
        return self._value_

    @property
    def spec(self) -> EventSpec:
        return self._spec

    @property
    def category(self) -> AuditCategory:
        return self._spec.category

    @property
    def title(self) -> str:
        return self._spec.title

    @property
    def params(self) -> Dict[str, Any]:
        return self._spec.params

    # authentication
    USER_LOGIN = ("user.login", event(_A, "User logged in", **_USER))
    AUTH_CERTIFICATE_LOGIN = ("auth.certificate_login", event(
        _A, "Certificate login attempt", certificate_cn=str, certificate_serial=str, matched=bool,
        certificate_id=Optional[int]))
    SESSION_LOGOUT = ("session.logout", event(_A, "User logged out", **_USER))

    # access
    SESSION_END = ("session.end", event(
        _X, "Session ended", username=str, cause=str, session_count=Optional[int], ended_session_id=Optional[str]))
    USER_CREATE = ("user.create", event(_X, "User created", username=str, user_type=str, via=Optional[str]))
    USER_UPDATE = ("user.update", event(_X, "User updated", **_USER))
    USER_ARCHIVE = ("user.archive", event(_X, "User archived", **_USER))
    USER_RESTORE = ("user.restore", event(_X, "User restored", **_USER))
    ROLE_ASSIGNMENT_CREATE = ("role_assignment.create", event(
        _X, "Role assigned at a node", node_name=str, **_ASSIGN))
    ROLE_ASSIGNMENT_DELETE = ("role_assignment.delete", event(
        _X, "Role assignment removed from a node", node_name=str, **_ASSIGN))
    SYSTEM_ROLE_ASSIGNMENT_CREATE = ("system_role_assignment.create", event(_X, "System role assigned", **_ASSIGN))
    SYSTEM_ROLE_ASSIGNMENT_DELETE = ("system_role_assignment.delete", event(
        _X, "System role assignment removed", **_ASSIGN))
    USER_CERTIFICATE_ISSUE = ("user_certificate.issue", event(_X, "Certificate issued", **_CERT))
    USER_CERTIFICATE_UPLOAD = ("user_certificate.upload", event(_X, "Certificate uploaded", **_CERT))
    USER_CERTIFICATE_EXPORT = ("user_certificate.export", event(_X, "Certificate package exported", **_CERT))
    USER_CERTIFICATE_REVOKE = ("user_certificate.revoke", event(_X, "Certificate revoked", **_CERT))
    ROLE_CREATE = ("role.create", event(_X, "Role created", **_ROLE))
    ROLE_UPDATE = ("role.update", event(_X, "Role updated", **_ROLE))
    ROLE_ARCHIVE = ("role.archive", event(_X, "Role archived", **_ROLE))
    ROLE_RESTORE = ("role.restore", event(_X, "Role restored", **_ROLE))
    ROLE_CLONE = ("role.clone", event(_X, "Role cloned", **_ROLE))
    ROLE_DELETE = ("role.delete", event(_X, "Role deleted permanently", **_ROLE))
    ROLE_PERMISSION_ADD = ("role.permission_add", event(_X, "Permission added to a role", **_ROLE))
    ROLE_PERMISSION_REMOVE = ("role.permission_remove", event(_X, "Permission removed from a role", **_ROLE))

    # structure
    POST_CREATE = ("post.create", event(_S, "Post created", **_POST))
    POST_UPDATE = ("post.update", event(_S, "Post updated", **_POST))
    POST_ARCHIVE = ("post.archive", event(_S, "Post archived", **_POST))
    POST_RESTORE = ("post.restore", event(_S, "Post restored", **_POST))
    SCOPE_GROUP_CREATE = ("scope_group.create", event(_S, "Scope group created", **_GROUP))
    SCOPE_GROUP_UPDATE = ("scope_group.update", event(_S, "Scope group updated", **_GROUP))
    SCOPE_GROUP_ARCHIVE = ("scope_group.archive", event(_S, "Scope group archived", **_GROUP))
    SCOPE_GROUP_RESTORE = ("scope_group.restore", event(_S, "Scope group restored", **_GROUP))
    OBSERVATION_UNITS_FILL_HISTORY = ("observation_units.fill_history", event(
        _S, "Unit history filled", observation_count=int, row_count=int))

    # data
    OBSERVATION_CREATE = ("observation.create", event(_D, "Observation created", **_OBS))
    OBSERVATION_UPDATE = ("observation.update", event(_D, "Observation updated", **_OBS))
    OBSERVATION_DISAPPEAR = ("observation.disappear", event(_D, "Observation marked as disappeared", **_OBS))
    OBSERVATION_ARCHIVE = ("observation.archive", event(_D, "Observation archived", **_OBS))
    OBSERVATION_RESTORE = ("observation.restore", event(_D, "Observation restored", **_OBS))
    OBSERVATION_FREQUENCY_CREATE = ("observation_frequency.create", event(_D, "Frequency band added", **_FREQ))
    OBSERVATION_FREQUENCY_UPDATE = ("observation_frequency.update", event(_D, "Frequency band updated", **_FREQ))
    OBSERVATION_FREQUENCY_ARCHIVE = ("observation_frequency.archive", event(_D, "Frequency band archived", **_FREQ))
    OBSERVATION_FREQUENCY_RESTORE = ("observation_frequency.restore", event(_D, "Frequency band restored", **_FREQ))
    MEDIA_CREATE = ("media.create", event(_D, "Media uploaded", observation_id=int, file_name=str, media_type=str))
    MEDIA_ARCHIVE = ("media.archive", event(_D, "Media archived", **_MEDIA))
    MEDIA_RESTORE = ("media.restore", event(_D, "Media restored", **_MEDIA))
    MEDIA_SHARE = ("media.share", event(_D, "Media shared by link", **_MEDIA))
    UPLOAD_SESSION_CREATE = ("upload_session.create", event(
        _D, "Upload session opened", upload_id=str, media_type=Optional[str]))
    UPLOAD_SESSION_ABORT = ("upload_session.abort", event(
        _D, "Upload session aborted", upload_id=str, media_type=Optional[str]))

    # dictionaries
    CATEGORY_CREATE = ("category.create", event(_DI, "Category created", **_NAME))
    CATEGORY_UPDATE = ("category.update", event(_DI, "Category updated", **_NAME))
    CATEGORY_ARCHIVE = ("category.archive", event(_DI, "Category archived", **_NAME))
    CATEGORY_RESTORE = ("category.restore", event(_DI, "Category restored", **_NAME))
    TAG_CREATE = ("tag.create", event(_DI, "Tag created", **_NAME))
    TAG_UPDATE = ("tag.update", event(_DI, "Tag updated", **_NAME))
    TAG_ARCHIVE = ("tag.archive", event(_DI, "Tag archived", **_NAME))
    TAG_RESTORE = ("tag.restore", event(_DI, "Tag restored", **_NAME))
    SIGNAL_TYPE_CREATE = ("signal_type.create", event(_DI, "Signal type created", signal_type_code=str))
    SIGNAL_TYPE_UPDATE = ("signal_type.update", event(_DI, "Signal type updated", signal_type_code=str))
    SIGNAL_TYPE_ARCHIVE = ("signal_type.archive", event(_DI, "Signal type archived", signal_type_code=str))
    SIGNAL_TYPE_RESTORE = ("signal_type.restore", event(_DI, "Signal type restored", signal_type_code=str))
    MEDIA_FORMAT_CREATE = ("media_format.create", event(_DI, "Media format created", content_type=str))
    MEDIA_FORMAT_UPDATE = ("media_format.update", event(_DI, "Media format updated", content_type=str))
    MEDIA_FORMAT_ARCHIVE = ("media_format.archive", event(_DI, "Media format archived", content_type=str))
    MEDIA_FORMAT_RESTORE = ("media_format.restore", event(_DI, "Media format restored", content_type=str))

    # reads
    SEARCH_OBSERVATIONS = ("search.observations", event(_R, "Observations searched", **_SEARCH))
    SEARCH_USERS = ("search.users", event(_R, "Users searched", **_SEARCH))
    SEARCH_POSTS = ("search.posts", event(_R, "Posts searched", **_SEARCH))
    SEARCH_ROLES = ("search.roles", event(_R, "Roles searched", **_SEARCH))
    SEARCH_CATEGORIES = ("search.categories", event(_R, "Categories searched", **_SEARCH))
    SEARCH_SIGNAL_TYPES = ("search.signal_types", event(_R, "Signal types searched", **_SEARCH))
    SEARCH_TAGS = ("search.tags", event(_R, "Tags searched", **_SEARCH))
    SEARCH_MEDIA = ("search.media", event(_R, "Media searched", **_SEARCH))
    SEARCH_MEDIA_FORMATS = ("search.media_formats", event(_R, "Media formats searched", **_SEARCH))

    # system
    MEDIA_POSTERS_RECONCILE = ("media_posters.reconcile", event(
        _SY, "Video posters reconciled", failed_abandoned=int, queued_untracked=int, requeued_missing=int,
        retried_failed=int))
    DATABASE_RECONCILE = ("database.reconcile", event(
        _SY, "Files reconciled after a database restore", orphan_files=int, stale_derived=int,
        returned_originals=int, missing_originals=int, orphan_packages=int, failed=int))
    LEGACY_UNKNOWN = ("legacy.unknown", event(
        _SY, "Event recorded before the event catalog", action=str, resource_type=str, message=Optional[str]))
    LEGACY_MIGRATE = ("legacy.migrate", event(
        _SY, "Rows written by a data migration", user_id=Optional[int], role_id=Optional[int]))

    # audit (a restore replaces the audit table itself)
    DATABASE_RESTORE = ("database.restore", event(
        _AU, "Database restored from a backup", backup_id=str, label=str, backup_revision=str, **_COUNTS))
    DATABASE_UNDO_RESTORE = ("database.undo_restore", event(
        _AU, "Database restore undone", source_schema=str, **_COUNTS))


def audit_event(code: str) -> Optional[AuditEvent]:
    try:
        return AuditEvent(code)
    except ValueError:
        return None
