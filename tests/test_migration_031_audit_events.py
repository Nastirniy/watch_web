"""031_audit_events: event, category, request_id, scope and params columns on audit_logs; the backfill of event and
category from (resource_type, action) through the frozen mapping (migrations/support/audit_events_v1.py) and of the
scope where it is cheap and exact; batches of 10,000 ids; a second run changes nothing it already wrote."""
import json
import sqlite3
from pathlib import Path

import pytest
from alembic.script import ScriptDirectory

from app.backend.enums.audit import AuditEvent, audit_event
from tests.helpers import audit_params_mismatch
from tests.test_column_bounds import _run_downgrade
from tests.test_migrations import HEAD, PROJECT_ROOT, run_migrate

pytestmark = pytest.mark.slow

REVISION = "031_audit_events"
PREVIOUS = "030_authorization_v3"
STAMP = "2026-10-01 10:00:00"
NEW_COLUMNS = {"event", "category", "request_id", "scope_key", "scope_post_id", "scope_group_id", "params"}
NEW_INDEXES = {"ix_audit_logs_event", "ix_audit_logs_request_id", "ix_audit_logs_scope_post_id",
               "ix_audit_logs_scope_group_id", "ix_audit_logs_category_created", "ix_audit_logs_scope_created"}
FAR_ID = 25_003     # a row two batches away from the others

# id: (action, resource_type, resource_id, details, old_values, new_values)
ROWS = {
    1: ("create", "observation", 11, {"message": "User a created observation #11"}, None, None),
    2: ("update", "post", 7, {"message": "User a updated post 'North'"}, None, None),
    3: ("search", "observations", None, {"message": "User a searched for observations with 2 results"}, None, None),
    4: ("create", "user_post_access", 3, {"message": "an old event"}, None, None),
    5: ("migrate", "user", 3, {"authorization_v3": {}}, None, None),
    6: ("update", "observation_frequency", 21, None, None, None),
    7: ("archive", "media", 31, None, None, None),
    8: ("archive", "scope_group", 4, None, None, None),
    9: ("create", "role_assignment", 41, None, None,
        {"id": 41, "scope_key": "g:4", "scope_group_id": 4, "scope_post_id": None}),
    10: ("delete", "role_assignment", 42, None,
         {"id": 42, "scope_key": "p:7", "scope_group_id": None, "scope_post_id": 7}, None),
    11: ("create", "role_assignment", 43, None, None, None),    # no snapshot: the assignment still exists
    12: ("create", "observation", 99, None, None, None),        # the observation is gone: no scope
    13: ("force_logout", "session", None, None, None, None),
    14: ("login", "user", 3, None, None, None),
    15: ("delete", "role", 5, None, None, None),
    16: ("migrate", "role", 5, None, None, None),
    17: ("restore", "database", None, {"backup_id": "b1"}, None, None),
    18: ("restore", "database", None, {"backup_id": "b2", "label": "scheduled", "backup_revision": "029_x",
                                       "kept_schema": "db_before_1", "revocations": 1, "certificates_added": 2,
                                       "users_added": 3, "crl_number": 4, "sessions_ended": 5, "ids_raised": 6},
         None, None),
    19: ("undo_restore", "database", None, {"source_schema": "db_before_1", "kept_schema": "db_before_2",
                                            "revocations": 0, "certificates_added": 0, "users_added": 0,
                                            "crl_number": 4, "sessions_ended": 2, "ids_raised": 9}, None, None),
    20: ("reconcile", "database", None, {"orphan_files": 1, "stale_derived": 2, "returned_originals": 0,
                                         "missing_originals": 3, "orphan_packages": 0, "failed": 0}, None, None),
    21: ("reconcile", "media_posters", None, {"message": "User a reconciled posters", "failed_abandoned": 1,
                                              "queued_untracked": 2, "requeued_missing": 3, "retried_failed": 0},
         None, None),
    # a backup taken before 031, restored by the code of 031: the restore tool wrote the catalog's names to details
    22: ("restore", "database", None, {"backup_id": "b3", "backup_label": "manual", "backup_revision": "030_x",
                                       "kept_schema": "db_before_3", "revocation_count": 0,
                                       "added_certificate_count": 0, "added_user_count": 0, "crl_number": 1,
                                       "ended_session_count": 7, "raised_id_count": 2}, None, None),
    FAR_ID: ("create", "tag", 1, None, None, None),
}
# the params 031 copies from details (DETAILS_PARAMS): the catalog's names, message and unknown keys left out
DETAILS_BACKFILLED = {
    18: {"backup_id": "b2", "backup_label": "scheduled", "backup_revision": "029_x", "kept_schema": "db_before_1",
         "revocation_count": 1, "added_certificate_count": 2, "added_user_count": 3, "crl_number": 4,
         "ended_session_count": 5, "raised_id_count": 6},
    19: {"source_schema": "db_before_1", "kept_schema": "db_before_2", "revocation_count": 0,
         "added_certificate_count": 0, "added_user_count": 0, "crl_number": 4, "ended_session_count": 2,
         "raised_id_count": 9},
    20: dict(ROWS[20][3]),
    21: {"failed_abandoned": 1, "queued_untracked": 2, "requeued_missing": 3, "retried_failed": 0},
    22: dict(ROWS[22][3]),
}


def _columns(db_file: Path, table: str) -> set:
    conn = sqlite3.connect(db_file)
    names = {row[1] for row in conn.execute(f"pragma table_info({table})")}
    conn.close()
    return names


def _indexes(db_file: Path, table: str) -> set:
    conn = sqlite3.connect(db_file)
    names = {row[1] for row in conn.execute(f"pragma index_list({table})")}
    conn.close()
    return names


def _dump(value):
    return None if value is None else json.dumps(value)


def _database_at_030(tmp_path: Path) -> Path:
    db_file = tmp_path / "audit.db"
    first = run_migrate(db_file)
    assert first.returncode == 0, first.stdout + first.stderr
    down = _run_downgrade(db_file, PREVIOUS)
    assert down.returncode == 0, down.stdout + down.stderr
    assert not NEW_COLUMNS & _columns(db_file, "audit_logs")
    assert not NEW_INDEXES & _indexes(db_file, "audit_logs")
    conn = sqlite3.connect(db_file)
    conn.execute("delete from audit_logs")    # the rows 030 wrote on the empty database
    conn.execute("insert into scope_groups (id, name, parent_key, kind, is_deleted, created_at, updated_at)"
                 " values (4, 'East', 0, 'brigade', 0, ?, ?)", (STAMP, STAMP))
    conn.execute("insert into posts (id, name, code, is_active, is_deleted, created_at, updated_at, group_id)"
                 " values (7, 'North', 'N-1', 1, 0, ?, ?, 4)", (STAMP, STAMP))
    conn.execute("insert into posts (id, name, code, is_active, is_deleted, created_at, updated_at, group_id)"
                 " values (8, 'South', 'S-1', 1, 0, ?, ?, 4)", (STAMP, STAMP))
    conn.execute("insert into observations (id, user_id, post_id, signal_type_id, observed_at, status,"
                 " confidence_level, is_deleted, created_at, updated_at)"
                 " values (11, 1, 7, 1, ?, 'active', 'high', 0, ?, ?)", (STAMP, STAMP, STAMP))
    conn.execute("insert into observations (id, user_id, post_id, signal_type_id, observed_at, status,"
                 " confidence_level, is_deleted, created_at, updated_at)"
                 " values (12, 1, 8, 1, ?, 'active', 'high', 0, ?, ?)", (STAMP, STAMP, STAMP))
    conn.execute("insert into observation_frequencies (id, observation_id, kind, frequency_min, frequency_max,"
                 " frequency_center, is_deleted, created_at, updated_at) values (21, 12, 'main', 1, 2, 1.5, 0, ?, ?)",
                 (STAMP, STAMP))
    conn.execute("insert into media_files (id, observation_id, original_filename, stored_filename, file_path,"
                 " file_size, mime_type, file_extension, media_type, file_hash, poster_attempts, uploaded_at,"
                 " is_deleted) values (31, 12, 'a.jpg', 'a.jpg', 'a.jpg', 1, 'image/jpeg', '.jpg', 'image', 'h', 0,"
                 " ?, 1)", (STAMP,))
    conn.execute("insert into role_assignments (id, user_id, role_id, scope_group_id, scope_post_id, scope_key,"
                 " created_at) values (43, 3, 2, null, 8, 'p:8', ?)", (STAMP,))
    for row_id, (action, resource_type, resource_id, details, old, new) in ROWS.items():
        conn.execute("insert into audit_logs (id, user_id, action, resource_type, resource_id, details, old_values,"
                     " new_values, success, created_at) values (?, 1, ?, ?, ?, ?, ?, ?, 1, ?)",
                     (row_id, action, resource_type, resource_id, _dump(details), _dump(old), _dump(new), STAMP))
    conn.commit()
    conn.close()
    return db_file


def _audit_rows(db_file: Path) -> dict:
    conn = sqlite3.connect(db_file)
    rows = {row[0]: row[1:] for row in conn.execute(
        "select id, event, category, scope_key, scope_post_id, scope_group_id, params, details from audit_logs")}
    conn.close()
    return {row_id: (event, category, key, post, group, json.loads(params) if params else None,
                     json.loads(details) if details else None)
            for row_id, (event, category, key, post, group, params, details) in rows.items()}


def test_031_is_in_the_chain():
    scripts = ScriptDirectory(str(PROJECT_ROOT / "app" / "backend" / "migrations"))
    assert scripts.get_revision(REVISION).down_revision == PREVIOUS
    assert scripts.get_revision(HEAD) is not None


def test_migration_backfills_events_and_post_scopes(tmp_path):
    db_file = _database_at_030(tmp_path)
    up = run_migrate(db_file)
    assert up.returncode == 0, up.stdout + up.stderr
    assert NEW_COLUMNS <= _columns(db_file, "audit_logs")
    assert NEW_INDEXES <= _indexes(db_file, "audit_logs")

    rows = _audit_rows(db_file)
    expected = {   # id: (event, category, scope_key, scope_post_id, scope_group_id)
        1: ("observation.create", "data", "p:7", 7, None),
        2: ("post.update", "structure", "p:7", 7, None),
        3: ("search.observations", "reads", None, None, None),
        4: ("legacy.unknown", "system", None, None, None),
        5: ("legacy.migrate", "system", None, None, None),
        6: ("observation_frequency.update", "data", "p:8", 8, None),
        7: ("media.archive", "data", "p:8", 8, None),
        8: ("scope_group.archive", "structure", "g:4", None, 4),
        9: ("role_assignment.create", "access", "g:4", None, 4),
        10: ("role_assignment.delete", "access", "p:7", 7, None),
        11: ("role_assignment.create", "access", "p:8", 8, None),
        12: ("observation.create", "data", None, None, None),
        13: ("session.end", "access", None, None, None),
        14: ("user.login", "authentication", None, None, None),
        15: ("role.delete", "access", None, None, None),
        16: ("legacy.migrate", "system", None, None, None),
        17: ("database.restore", "audit", None, None, None),
        18: ("database.restore", "audit", None, None, None),
        19: ("database.undo_restore", "audit", None, None, None),
        20: ("database.reconcile", "system", None, None, None),
        21: ("media_posters.reconcile", "system", None, None, None),
        22: ("database.restore", "audit", None, None, None),
        FAR_ID: ("tag.create", "dictionaries", None, None, None),
    }
    assert {row_id: row[:5] for row_id, row in rows.items()} == expected
    # params only where the event needs what the old columns hold; the old details stay untouched
    assert rows[4][5] == {"action": "create", "resource_type": "user_post_access", "message": "an old event"}
    assert rows[5][5] == {"user_id": 3}
    assert rows[16][5] == {"role_id": 5}
    assert rows[1][5] is None
    assert rows[1][6] == {"message": "User a created observation #11"}
    assert rows[17][6] == {"backup_id": "b1"}
    # restores and reconciles: their counts were details keys, now params that fit the event (row 17 lacks most of
    # them: the read API serves it as legacy.unknown with its details)
    assert rows[17][5] == {"backup_id": "b1"}
    for row_id, params in DETAILS_BACKFILLED.items():
        assert rows[row_id][5] == params, row_id
        assert audit_params_mismatch(audit_event(rows[row_id][0]), params) is None, row_id
        assert rows[row_id][6] == ROWS[row_id][3], row_id      # details untouched
    assert audit_params_mismatch(AuditEvent.DATABASE_RESTORE, rows[17][5]) is not None

    down = _run_downgrade(db_file, PREVIOUS)
    assert down.returncode == 0, down.stdout + down.stderr
    assert not NEW_COLUMNS & _columns(db_file, "audit_logs")
    assert not NEW_INDEXES & _indexes(db_file, "audit_logs")
    conn = sqlite3.connect(db_file)
    assert conn.execute("select count(*) from audit_logs").fetchone()[0] == len(ROWS)
    conn.close()


def test_migration_is_idempotent(tmp_path):
    """A second run of 031 over the columns it already added (the alembic version put back to 030) adds nothing,
    keeps what it wrote and fills only the rows written in between"""
    db_file = _database_at_030(tmp_path)
    up = run_migrate(db_file)
    assert up.returncode == 0, up.stdout + up.stderr
    conn = sqlite3.connect(db_file)
    conn.execute("update audit_logs set event = 'post.archive', scope_key = 'p:8', scope_post_id = 8 where id = 2")
    conn.execute("insert into audit_logs (id, user_id, action, resource_type, resource_id, success, created_at)"
                 " values (50, 1, 'update', 'post', 8, 1, ?)", (STAMP,))
    conn.execute("update alembic_version set version_num = ?", (PREVIOUS,))
    conn.commit()
    conn.close()
    before = _audit_rows(db_file)

    again = run_migrate(db_file)
    assert again.returncode == 0, again.stdout + again.stderr
    after = _audit_rows(db_file)
    assert after[2][:5] == ("post.archive", "structure", "p:8", 8, None)
    assert after[50][:5] == ("post.update", "structure", "p:8", 8, None)
    assert {k: v for k, v in after.items() if k != 50} == {k: v for k, v in before.items() if k != 50}
