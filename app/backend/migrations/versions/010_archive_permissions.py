"""Soft delete permissions are renamed from X.delete to X.archive.

Every role keeps exactly what it had: the permission rows are renamed in
place, so role_permissions rows still point at the same ids. If a target name
already exists (the application was started on the new code before this
revision ran), the assignments of the old row are merged into the new one and
the old row is removed.

role.delete changes meaning: it becomes the permanent delete of roles. The old
role.delete row (soft delete) is renamed to role.archive here, so no role
silently keeps the new, destructive permission. The fresh role.delete row is
inserted by seeding at application start and granted to the admin role only.

Display names and descriptions are refreshed by seeding from PERMISSION_META,
not here.

Revision ID: 010_archive_permissions
Revises: 009_media_playback_metadata
Create Date: 2026-09-10
"""
from typing import Dict, Optional

from alembic import op
import sqlalchemy as sa

revision = "010_archive_permissions"
down_revision = "009_media_playback_metadata"
branch_labels = None
depends_on = None

RENAMES = (
    ("user.delete", "user.archive"),
    ("post.delete", "post.archive"),
    ("observation.delete", "observation.archive"),
    ("role.delete", "role.archive"),
    ("category.delete", "category.archive"),
    ("signal_type.delete", "signal_type.archive"),
    ("tag.delete", "tag.archive"),
    ("media.delete", "media.archive"),
    ("media_format.delete", "media_format.archive"),
)

PERMANENT_ROLE_DELETE = "role.delete"


def _log(message: str) -> None:
    print(f"[010_archive_permissions] {message}")


def _permission_ids(bind) -> Dict[str, int]:
    rows = bind.execute(sa.text("SELECT id, name FROM permissions")).fetchall()
    return {name: row_id for row_id, name in rows}


def _rename_or_merge(bind, old_name: str, new_name: str) -> None:
    ids = _permission_ids(bind)
    old_id: Optional[int] = ids.get(old_name)
    new_id: Optional[int] = ids.get(new_name)
    if old_id is None:
        return
    if new_id is None:
        _log(f"renaming permission {old_name} -> {new_name}")
        bind.execute(
            sa.text("UPDATE permissions SET name = :new_name WHERE id = :old_id"),
            {"new_name": new_name, "old_id": old_id},
        )
        return

    # Both rows exist: move the assignments of the old row to the new one, then drop the old row
    _log(f"merging permission {old_name} (id {old_id}) into {new_name} (id {new_id})")
    role_ids_old = {
        row[0] for row in bind.execute(
            sa.text("SELECT role_id FROM role_permissions WHERE permission_id = :pid"), {"pid": old_id}
        ).fetchall()
    }
    role_ids_new = {
        row[0] for row in bind.execute(
            sa.text("SELECT role_id FROM role_permissions WHERE permission_id = :pid"), {"pid": new_id}
        ).fetchall()
    }
    for role_id in sorted(role_ids_old - role_ids_new):
        bind.execute(
            sa.text("INSERT INTO role_permissions (role_id, permission_id) VALUES (:rid, :pid)"),
            {"rid": role_id, "pid": new_id},
        )
    bind.execute(sa.text("DELETE FROM role_permissions WHERE permission_id = :pid"), {"pid": old_id})
    bind.execute(sa.text("DELETE FROM permissions WHERE id = :pid"), {"pid": old_id})


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "permissions" not in inspector.get_table_names():
        return
    for old_name, new_name in RENAMES:
        _rename_or_merge(bind, old_name, new_name)


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "permissions" not in inspector.get_table_names():
        return
    # The permanent delete permission has no meaning on the old code: drop it with its assignments
    ids = _permission_ids(bind)
    permanent_id = ids.get(PERMANENT_ROLE_DELETE)
    if permanent_id is not None and ids.get("role.archive") is not None:
        _log(f"dropping permanent {PERMANENT_ROLE_DELETE} (id {permanent_id})")
        bind.execute(sa.text("DELETE FROM role_permissions WHERE permission_id = :pid"), {"pid": permanent_id})
        bind.execute(sa.text("DELETE FROM permissions WHERE id = :pid"), {"pid": permanent_id})
    for old_name, new_name in RENAMES:
        _rename_or_merge(bind, new_name, old_name)
