"""Sync the database schema with the current SQLModel state.

Production tables were created by SQLModel.metadata.create_all before Alembic
was adopted, so no earlier revision describes them. This revision reconciles
any database with the models by ADDING what is missing:

- tables that do not exist
- columns that do not exist on existing tables
- indexes and unique constraints declared in the models

It never drops or alters anything. Column type differences are only reported.
Downgrade is a no-op because removing production data is not acceptable.

Revision ID: 002_sync_schema
Revises: 001_relativize_media_paths
Create Date: 2026-09-02
"""
import re
from typing import Dict, Set, FrozenSet

from alembic import op
import sqlalchemy as sa

revision = "002_sync_schema"

# Tables introduced by revisions AFTER this one. They are skipped here so the
# owning revision creates them (and can drop them on downgrade). Any later
# revision that creates a table must also be listed here, or guard its
# create_table with an inspector check, otherwise fresh installs fail.
TABLES_OWNED_BY_LATER_REVISIONS = {
    "user_post_access",  # 003_add_user_post_access
    "worker_heartbeats",  # 005_video_posters
    "media_formats",  # 008_media_formats
    "upload_sessions",  # 011_upload_sessions
    "observation_frequencies",  # 012_observation_frequencies
    "video_storyboards",  # 017_video_storyboards
    "scope_groups",  # 021_scope_groups
    "role_assignments",  # 023_authorization_v2
    "user_certificates",  # 024_user_certificates
    "certificate_authority_state",  # 024_user_certificates
}
down_revision = "001_relativize_media_paths"
branch_labels = None
depends_on = None


def _log(message: str) -> None:
    print(f"[002_sync_schema] {message}")


_INT_WIDTH = re.compile(r"^(TINYINT|SMALLINT|MEDIUMINT|INT|INTEGER|BIGINT)\((\d+)\)$")


def _normalize_type(compiled: str) -> str:
    """Reduce dialect spelling differences so equivalent types compare equal."""
    value = compiled.upper().strip()
    if value in ("BOOL", "BOOLEAN", "TINYINT(1)"):
        return "BOOL"
    match = _INT_WIDTH.match(value)
    if match:
        value = match.group(1)
    if value == "INTEGER":
        value = "INT"
    if value.startswith("NUMERIC"):
        value = "DECIMAL" + value[len("NUMERIC"):]
    return value


def _compiled_type(column_type, bind) -> str:
    try:
        return _normalize_type(column_type.compile(dialect=bind.dialect))
    except Exception:  # pragma: no cover - defensive, never blocks the migration
        return _normalize_type(str(column_type))


def _sync_columns(bind, inspector, table: sa.Table) -> None:
    existing: Dict[str, dict] = {c["name"]: c for c in inspector.get_columns(table.name)}
    for column in table.columns:
        if column.name in existing:
            declared = _compiled_type(column.type, bind)
            actual = _compiled_type(existing[column.name]["type"], bind)
            if declared != actual:
                _log(
                    f"type differs on {table.name}.{column.name}: model={declared} db={actual} "
                    f"(informational, not changed)"
                )
            continue

        nullable = bool(column.nullable)
        if not nullable and column.server_default is None:
            _log(
                f"{table.name}.{column.name} is NOT NULL without a server default; "
                f"added as NULLABLE to keep existing rows valid. Fill it and tighten in a follow-up migration."
            )
            nullable = True

        _log(f"adding missing column {table.name}.{column.name}")
        op.add_column(
            table.name,
            sa.Column(
                column.name,
                column.type,
                nullable=nullable,
                server_default=column.server_default,
            ),
        )
        for fk in column.foreign_keys:
            _log(
                f"{table.name}.{column.name} declares a foreign key to {fk.target_fullname}; "
                f"the constraint was not created (add it in a dedicated migration if required)"
            )


def _existing_index_signatures(inspector, table_name: str) -> Dict[str, FrozenSet[str]]:
    result: Dict[str, FrozenSet[str]] = {}
    for idx in inspector.get_indexes(table_name):
        cols = [c for c in idx.get("column_names", []) if c]
        if idx.get("name"):
            result[idx["name"]] = frozenset(cols)
    return result


def _sync_indexes(inspector, table: sa.Table) -> None:
    existing = _existing_index_signatures(inspector, table.name)
    existing_signatures: Set[FrozenSet[str]] = set(existing.values())
    for index in table.indexes:
        cols = frozenset(c.name for c in index.columns)
        if index.name in existing or cols in existing_signatures:
            continue
        _log(f"creating missing index {index.name} on {table.name}({', '.join(sorted(cols))})")
        op.create_index(index.name, table.name, [c.name for c in index.columns], unique=bool(index.unique))
        existing_signatures.add(cols)


def _sync_unique_constraints(inspector, table: sa.Table) -> None:
    existing_signatures: Set[FrozenSet[str]] = set()
    for uc in inspector.get_unique_constraints(table.name):
        existing_signatures.add(frozenset(c for c in uc.get("column_names", []) if c))
    for idx in inspector.get_indexes(table.name):
        if idx.get("unique"):
            existing_signatures.add(frozenset(c for c in idx.get("column_names", []) if c))

    for constraint in table.constraints:
        if not isinstance(constraint, sa.UniqueConstraint):
            continue
        cols = [c.name for c in constraint.columns]
        signature = frozenset(cols)
        if not cols or signature in existing_signatures:
            continue
        name = constraint.name or f"uq_{table.name}_{'_'.join(cols)}"
        _log(f"creating missing unique index {name} on {table.name}({', '.join(cols)})")
        # A unique index is portable across MySQL and sqlite (sqlite cannot ALTER constraints).
        op.create_index(name, table.name, cols, unique=True)
        existing_signatures.add(signature)


def upgrade() -> None:
    import app.backend.models  # noqa: F401  registers every table on SQLModel.metadata
    from sqlmodel import SQLModel

    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing_tables = set(inspector.get_table_names())

    for table in SQLModel.metadata.sorted_tables:
        if table.name in TABLES_OWNED_BY_LATER_REVISIONS:
            continue
        if table.name not in existing_tables:
            # A foreign key to a table owned by a later revision (posts.group_id -> scope_groups) needs its target
            for fk in table.foreign_keys:
                target = fk.column.table
                if target.name in TABLES_OWNED_BY_LATER_REVISIONS and target.name not in existing_tables:
                    _log(f"creating {target.name} first: {table.name} refers to it")
                    target.create(bind, checkfirst=True)
                    existing_tables.add(target.name)
            _log(f"creating missing table {table.name}")
            table.create(bind, checkfirst=True)
            existing_tables.add(table.name)
            continue
        _sync_columns(bind, inspector, table)
        _sync_indexes(inspector, table)
        _sync_unique_constraints(inspector, table)

    _log("schema sync finished")


def downgrade() -> None:
    # Intentionally a no-op: this revision only adds missing schema objects and
    # must never remove production data.
    pass
