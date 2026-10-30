"""The audit events of the rows written before the event catalog (enums/audit.py): (resource_type, action) -> (event
code, category). FROZEN once migration 031_audit_events has shipped: 031 backfills old rows with it. The mapping is
append-only: a later catalog event adds its own new pair at the end; existing pairs never change (no row written
before 031 has a new pair, so the backfill of the old rows stays the same). Never import the
catalog here; tests/test_audit_catalog.py checks that every code below is a catalog event of that category and that
DETAILS_PARAMS names only params of its event.

Search rows carry the PLURAL entity as resource_type: they map to search.<entity>. The rows of the data migrations
023 / 030 (action migrate) map to legacy.migrate. A pair not listed (written by code that no longer exists, such as
user_post_access or a hard delete before the archive rule) is legacy.unknown.

The pairs the catalog's writers produce (resource_type and action of a new-form row, e.g. session / end, the singular
search pairs) are listed too: after a downgrade of 031 and an upgrade again, the rows written in between map back to
their events (their params are gone with the downgrade; the read API serves them as legacy.unknown).

DETAILS_PARAMS: the events whose old rows kept their facts as keys of details (counts of restores and reconciles):
the backfill copies those keys into params, renamed to the catalog's names (old key -> param). A key that already has
the catalog's name maps to itself (a restore of an older backup writes the new names into details of a schema
without params; the next start backfills them).
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
    # the pairs of the catalog's writers that differ from the old ones
    ("session", "end"): ("session.end", "access"),
    ("role", "clone"): ("role.clone", "access"),
    ("role", "permission_add"): ("role.permission_add", "access"),
    ("role", "permission_remove"): ("role.permission_remove", "access"),
    ("observation", "disappear"): ("observation.disappear", "data"),
    ("auth", "certificate_login"): ("auth.certificate_login", "authentication"),
    **{(entity, "search"): (f"search.{plural}", "reads") for plural, entity in (
        ("observations", "observation"), ("users", "user"), ("posts", "post"), ("roles", "role"),
        ("categories", "category"), ("signal_types", "signal_type"), ("tags", "tag"),
        ("media_formats", "media_format"))},     # media: singular and plural are the same word
    # the pairs of catalog events added after 031 (no row written before 031 has them, so the backfill of the old
    # rows is unchanged; rows written by them map back after a downgrade of 031 and an upgrade again)
    **_events("user_guide", "dictionaries", *_CRUD),
    **_events("user_guide_file", "dictionaries", "upload", "archive", "restore"),
    ("session", "certificate_mismatch"): ("session.certificate_mismatch", "authentication"),
}

_RESTORE_COUNTS = {
    "kept_schema": "kept_schema", "crl_number": "crl_number",
    "revocations": "revocation_count", "revocation_count": "revocation_count",
    "certificates_added": "added_certificate_count", "added_certificate_count": "added_certificate_count",
    "users_added": "added_user_count", "added_user_count": "added_user_count",
    "sessions_ended": "ended_session_count", "ended_session_count": "ended_session_count",
    "ids_raised": "raised_id_count", "raised_id_count": "raised_id_count",
}

DETAILS_PARAMS: Dict[str, Dict[str, str]] = {
    "database.restore": {"backup_id": "backup_id", "label": "backup_label", "backup_label": "backup_label",
                         "backup_revision": "backup_revision", **_RESTORE_COUNTS},
    "database.undo_restore": {"source_schema": "source_schema", **_RESTORE_COUNTS},
    "database.reconcile": {name: name for name in (
        "orphan_files", "stale_derived", "returned_originals", "missing_originals", "orphan_packages", "failed")},
    "media_posters.reconcile": {name: name for name in (
        "failed_abandoned", "queued_untracked", "requeued_missing", "retried_failed")},
}
