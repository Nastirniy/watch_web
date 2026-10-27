"""Audit events: event, category, request id, scope and params on every audit row (part 6, decision 2).

audit_logs gets event VARCHAR(64) (index), category VARCHAR(24), request_id VARCHAR(32) (index), scope_key
VARCHAR(24), scope_post_id INT (index), scope_group_id INT (index), params JSON, all nullable, and the composite
indexes ix_audit_logs_category_created (category, created_at) and ix_audit_logs_scope_created (scope_key,
created_at). No foreign keys: rows outlive what they name. Every add is guarded with an inspector check (002 builds
audit_logs from the models on a fresh install, with the columns already there).

Backfill, in batches of 10,000 ids, only rows whose event is still null (a second run changes nothing it wrote):
- event and category from (resource_type, action) through the frozen mapping migrations/support/audit_events_v1.py
  (plural search resource types -> search.<entity>, migrate -> legacy.migrate, anything else -> legacy.unknown);
  params for legacy.unknown ({action, resource_type, message?}) and legacy.migrate ({user_id} / {role_id}), and for
  the events whose old rows kept their facts as keys of details (database.restore, database.undo_restore,
  database.reconcile, media_posters.reconcile: DETAILS_PARAMS, the keys renamed to the catalog's names, message and
  unknown keys left out); details stays untouched (the read API serves it with legacy.unknown rows);
- the scope where it is cheap (rows whose scope_key is null): observation / observation_frequency / media rows
  through the CURRENT post of their observation (an observation moved since keeps the post it has now), post rows by
  resource_id, scope_group rows by resource_id, role_assignment rows from the assignment snapshot the row holds
  (new_values of a create, old_values of a delete), else from the assignment if it still exists. Everything else
  stays null: system-wide, only audit.view sees it.
A backup taken before 031 and restored later comes back at 030: the restore writes its row's facts into details
(services/backups/restore.py) and the next start runs 031 over it, which backfills that row the same way.
Correlated-subquery UPDATEs only (no UPDATE ... JOIN): SQLite and MariaDB run the same statements. The backfill runs
in an autocommit block (every statement commits on its own) and never past the highest id it read at its start.

Downgrade drops the indexes and the columns (batch mode on SQLite).

Revision ID: 031_audit_events
Revises: 030_authorization_v3
Create Date: 2026-10-27
"""
import re
from typing import Dict, Optional

from alembic import op
import sqlalchemy as sa

from app.backend.migrations.support.audit_events_v1 import DETAILS_PARAMS, LEGACY_EVENTS, LEGACY_MIGRATE, LEGACY_UNKNOWN

revision = "031_audit_events"
down_revision = "030_authorization_v3"
branch_labels = None
depends_on = None

TABLE = "audit_logs"
BATCH = 10_000
COLUMNS = (
    ("event", lambda: sa.String(length=64)),
    ("category", lambda: sa.String(length=24)),
    ("request_id", lambda: sa.String(length=32)),
    ("scope_key", lambda: sa.String(length=24)),
    ("scope_post_id", sa.Integer),
    ("scope_group_id", sa.Integer),
    ("params", sa.JSON),
)
INDEXES = (
    ("ix_audit_logs_event", ("event",)),
    ("ix_audit_logs_request_id", ("request_id",)),
    ("ix_audit_logs_scope_post_id", ("scope_post_id",)),
    ("ix_audit_logs_scope_group_id", ("scope_group_id",)),
    ("ix_audit_logs_category_created", ("category", "created_at")),
    ("ix_audit_logs_scope_created", ("scope_key", "created_at")),
)
SCOPE_KEY = re.compile(r"^(?:root|([gp]):([1-9][0-9]*))$")

AUDIT = sa.table(
    TABLE, sa.column("id", sa.Integer), sa.column("action", sa.String), sa.column("resource_type", sa.String),
    sa.column("resource_id", sa.Integer), sa.column("details", sa.JSON), sa.column("old_values", sa.JSON),
    sa.column("new_values", sa.JSON), sa.column("event", sa.String), sa.column("category", sa.String),
    sa.column("params", sa.JSON), sa.column("scope_key", sa.String), sa.column("scope_post_id", sa.Integer),
    sa.column("scope_group_id", sa.Integer),
)
OBSERVATIONS = sa.table("observations", sa.column("id", sa.Integer), sa.column("post_id", sa.Integer))
FREQUENCIES = sa.table("observation_frequencies", sa.column("id", sa.Integer), sa.column("observation_id", sa.Integer))
MEDIA = sa.table("media_files", sa.column("id", sa.Integer), sa.column("observation_id", sa.Integer))
ASSIGNMENTS = sa.table("role_assignments", sa.column("id", sa.Integer), sa.column("scope_key", sa.String),
                       sa.column("scope_post_id", sa.Integer), sa.column("scope_group_id", sa.Integer))


def _log(message: str) -> None:
    print(f"[031_audit_events] {message}")


def _inspector():
    return sa.inspect(op.get_bind())


def _columns() -> set:
    return {c["name"] for c in _inspector().get_columns(TABLE)}


def _indexes() -> set:
    return {i["name"] for i in _inspector().get_indexes(TABLE)}


def upgrade() -> None:
    if TABLE not in set(_inspector().get_table_names()):
        return
    present = _columns()
    for name, column_type in COLUMNS:
        if name not in present:
            op.add_column(TABLE, sa.Column(name, column_type(), nullable=True))
    indexes = _indexes()
    for name, columns in INDEXES:
        if name not in indexes:
            op.create_index(name, TABLE, list(columns))
    # Every statement of the backfill commits on its own: no transaction holds a read view across the batches. On
    # MariaDB with innodb_snapshot_isolation an UPDATE of a long transaction fails (1020) as soon as its range scan
    # meets a row inserted after the transaction's first read (an old writer still running, measured on 11.8), and
    # its locks would hold such writers until the end. A run that stops halfway is completed by the next one (only
    # rows whose event is still null are filled).
    with op.get_context().autocommit_block():
        _backfill(op.get_bind())


def downgrade() -> None:
    if TABLE not in set(_inspector().get_table_names()):
        return
    indexes = _indexes()
    for name, _columns_of in INDEXES:
        if name in indexes:
            op.drop_index(name, table_name=TABLE)
    present = _columns()
    dropped = [name for name, _ in COLUMNS if name in present]
    if dropped:
        with op.batch_alter_table(TABLE) as batch:
            for name in dropped:
                batch.drop_column(name)


# --- backfill ------------------------------------------------------------------------------------------------------

def _backfill(conn) -> None:
    low, high = conn.execute(sa.select(sa.func.min(AUDIT.c.id), sa.func.max(AUDIT.c.id))).one()
    if low is None:
        return
    tables = set(sa.inspect(conn).get_table_names())
    start, batches = low, 0
    while start <= high:
        # never past the highest id read above: a row inserted meanwhile (an old writer still running) is not this
        # run's (it keeps event null, served as legacy.unknown)
        end = min(start + BATCH - 1, high)
        in_batch = AUDIT.c.id.between(start, end)
        _events(conn, in_batch)
        _legacy_params(conn, in_batch)
        _details_params(conn, in_batch)
        _scopes(conn, in_batch, tables)
        start, batches = end + 1, batches + 1
    _log(f"backfilled ids {low}..{high} in {batches} batches")


def _pair(resource_type: str, action: str):
    return sa.and_(AUDIT.c.resource_type == resource_type, AUDIT.c.action == action)


def _events(conn, in_batch) -> None:
    pairs = sorted(LEGACY_EVENTS.items())
    event = sa.case(*[(_pair(*pair), code) for pair, (code, _) in pairs], else_=LEGACY_UNKNOWN[0])
    category = sa.case(*[(_pair(*pair), category) for pair, (_, category) in pairs], else_=LEGACY_UNKNOWN[1])
    conn.execute(sa.update(AUDIT).where(in_batch, AUDIT.c.event.is_(None)).values(event=event, category=category))


def _legacy_params(conn, in_batch) -> None:
    """legacy.unknown and legacy.migrate rows take the params their message needs from the old columns"""
    rows = conn.execute(
        sa.select(AUDIT.c.id, AUDIT.c.event, AUDIT.c.action, AUDIT.c.resource_type, AUDIT.c.resource_id,
                  AUDIT.c.details)
        .where(in_batch, AUDIT.c.event.in_([LEGACY_UNKNOWN[0], LEGACY_MIGRATE[0]]), AUDIT.c.params.is_(None))
    ).all()
    for row_id, event, action, resource_type, resource_id, details in rows:
        if event == LEGACY_UNKNOWN[0]:
            params = {"action": action, "resource_type": resource_type}
            message = details.get("message") if isinstance(details, dict) else None
            if isinstance(message, str):
                params["message"] = message
        else:
            name = {"user": "user_id", "role": "role_id"}.get(resource_type)
            params = {name: resource_id} if name and resource_id is not None else {}
        conn.execute(sa.update(AUDIT).where(AUDIT.c.id == row_id).values(params=params))


def details_params(code: str, details) -> Optional[dict]:
    """The params of an old row of an event in DETAILS_PARAMS: the keys of details it lists, renamed; None when there
    is none of them"""
    names = DETAILS_PARAMS.get(code, {})
    if not isinstance(details, dict):
        return None
    params = {names[key]: value for key, value in details.items() if key in names}
    return params or None


def _details_params(conn, in_batch) -> None:
    """Old rows of restores and reconciles: their counts were keys of details, now params"""
    rows = conn.execute(
        sa.select(AUDIT.c.id, AUDIT.c.event, AUDIT.c.details)
        .where(in_batch, AUDIT.c.event.in_(sorted(DETAILS_PARAMS)), AUDIT.c.params.is_(None))
    ).all()
    for row_id, code, details in rows:
        params = details_params(code, details)
        if params is not None:
            conn.execute(sa.update(AUDIT).where(AUDIT.c.id == row_id).values(params=params))


def _no_scope(resource_type: str, in_batch):
    return sa.and_(in_batch, AUDIT.c.resource_type == resource_type, AUDIT.c.scope_key.is_(None),
                   AUDIT.c.resource_id.isnot(None))


def _scopes(conn, in_batch, tables: set) -> None:
    resource = AUDIT.c.resource_id
    if "observations" in tables:
        post_of = {
            "observation": sa.select(OBSERVATIONS.c.post_id).where(OBSERVATIONS.c.id == resource),
        }
        if "observation_frequencies" in tables:
            post_of["observation_frequency"] = (
                sa.select(OBSERVATIONS.c.post_id)
                .select_from(FREQUENCIES.join(OBSERVATIONS, OBSERVATIONS.c.id == FREQUENCIES.c.observation_id))
                .where(FREQUENCIES.c.id == resource))
        if "media_files" in tables:
            post_of["media"] = (
                sa.select(OBSERVATIONS.c.post_id)
                .select_from(MEDIA.join(OBSERVATIONS, OBSERVATIONS.c.id == MEDIA.c.observation_id))
                .where(MEDIA.c.id == resource))
        for resource_type, query in post_of.items():
            conn.execute(sa.update(AUDIT).where(_no_scope(resource_type, in_batch))
                         .values(scope_post_id=query.scalar_subquery()))
    conn.execute(sa.update(AUDIT).where(_no_scope("post", in_batch)).values(scope_post_id=resource))
    conn.execute(sa.update(AUDIT).where(_no_scope("scope_group", in_batch)).values(scope_group_id=resource))
    _assignment_scopes(conn, in_batch, tables)
    # the key of the id found above (concatenation compiles to || on SQLite, concat() on MariaDB)
    for prefix, column in (("p:", AUDIT.c.scope_post_id), ("g:", AUDIT.c.scope_group_id)):
        conn.execute(sa.update(AUDIT).where(in_batch, AUDIT.c.scope_key.is_(None), column.isnot(None))
                     .values(scope_key=sa.literal(prefix, sa.String) + sa.cast(column, sa.String)))


def _snapshot_scope(snapshot) -> Optional[Dict[str, object]]:
    """The scope columns of an assignment snapshot, from its scope_key ("root", "g:<id>", "p:<id>")"""
    key = snapshot.get("scope_key") if isinstance(snapshot, dict) else None
    match = SCOPE_KEY.match(key) if isinstance(key, str) else None
    if match is None:
        return None
    kind, number = match.groups()
    return {"scope_key": key, "scope_group_id": int(number) if kind == "g" else None,
            "scope_post_id": int(number) if kind == "p" else None}


def _assignment_scopes(conn, in_batch, tables: set) -> None:
    rows = conn.execute(
        sa.select(AUDIT.c.id, AUDIT.c.action, AUDIT.c.old_values, AUDIT.c.new_values)
        .where(_no_scope("role_assignment", in_batch))
    ).all()
    for row_id, action, old_values, new_values in rows:
        scope = _snapshot_scope(old_values if action == "delete" else new_values)
        if scope is not None:
            conn.execute(sa.update(AUDIT).where(AUDIT.c.id == row_id).values(**scope))
    if "role_assignments" not in tables:
        return
    still = sa.and_(_no_scope("role_assignment", in_batch), sa.exists().where(ASSIGNMENTS.c.id == AUDIT.c.resource_id))

    def of_assignment(column):
        return sa.select(column).where(ASSIGNMENTS.c.id == AUDIT.c.resource_id).scalar_subquery()

    conn.execute(sa.update(AUDIT).where(still).values(
        scope_key=of_assignment(ASSIGNMENTS.c.scope_key), scope_post_id=of_assignment(ASSIGNMENTS.c.scope_post_id),
        scope_group_id=of_assignment(ASSIGNMENTS.c.scope_group_id)))
