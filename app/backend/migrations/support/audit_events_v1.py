"""The audit events of the rows written before the event catalog (enums/audit.py): (resource_type, action) -> (event
code, category). FROZEN once migration 031_audit_events has shipped: 031 backfills old rows with it, and
create_audit_log maps the legacy keyword form (action=, resource_type=) through it until every writer names its
event. Never import the catalog here; tests/test_audit_catalog.py checks that every code below is a catalog event of
that category.

Search rows carry the PLURAL entity as resource_type: they map to search.<entity>. The rows of the data migrations
023 / 030 (action migrate) map to legacy.migrate. A pair not listed (written by code that no longer exists, such as
user_post_access or a hard delete before the archive rule) is legacy.unknown.
"""
from typing import Dict, Tuple

LEGACY_UNKNOWN: Tuple[str, str] = ("legacy.unknown", "system")
LEGACY_MIGRATE: Tuple[str, str] = ("legacy.migrate", "system")

_CRUD = ("create", "update", "archive", "restore")
_SEARCHED = ("observations", "users", "posts", "roles", "categories", "signal_types", "tags", "media",
             "media_formats")


def _events(resource_type: str, category: str, *actions: str) -> Dict[Tuple[str, str], Tuple[str, str]]:
    return {(resource_type, action): (f"{resource_type}.{action}", category) for action in actions}


LEGACY_EVENTS: Dict[Tuple[str, str], Tuple[str, str]] = {
    # authentication
    ("user", "login"): ("user.login", "authentication"),
    ("auth", "pki_auth_attempt"): ("auth.certificate_login", "authentication"),
    ("session", "logout"): ("session.logout", "authentication"),
    # access
    ("session", "force_logout"): ("session.end", "access"),
    **_events("user", "access", *_CRUD),
    **_events("role_assignment", "access", "create", "delete"),
    **_events("system_role_assignment", "access", "create", "delete"),
    **_events("user_certificate", "access", "issue", "upload", "export", "revoke"),
    **_events("role", "access", *_CRUD, "delete"),     # clone wrote create, permission changes wrote update
    # structure
    **_events("post", "structure", *_CRUD),
    **_events("scope_group", "structure", *_CRUD),
    **_events("observation_units", "structure", "fill_history"),
    # data
    **_events("observation", "data", *_CRUD),            # disappear wrote update
    **_events("observation_frequency", "data", *_CRUD),
    **_events("media", "data", "create", "archive", "restore", "share"),
    **_events("upload_session", "data", "create", "abort"),
    # dictionaries
    **_events("category", "dictionaries", *_CRUD),
    **_events("tag", "dictionaries", *_CRUD),
    **_events("signal_type", "dictionaries", *_CRUD),
    **_events("media_format", "dictionaries", *_CRUD),
    # reads
    **{(entity, "search"): (f"search.{entity}", "reads") for entity in _SEARCHED},
    # system
    **_events("media_posters", "system", "reconcile"),
    **_events("database", "system", "reconcile"),
    ("user", "migrate"): LEGACY_MIGRATE,
    ("role", "migrate"): LEGACY_MIGRATE,
    # audit: a restore replaces the audit table itself
    **_events("database", "audit", "restore", "undo_restore"),
}
