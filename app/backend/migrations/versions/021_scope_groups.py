"""Scope groups: nested groups of posts (the tree between the root and the posts).

Creates scope_groups (parent_id, parent_key = parent_id or 0 for the unique name per parent) and adds posts.group_id
with its index. A post without a group stays directly under the root. Nothing reads the tree for authorization yet.

Guarded with inspector checks: fresh installs built by 002_sync_schema from the models already have them (002 creates
scope_groups before posts when it creates posts).

Revision ID: 021_scope_groups
Revises: 020_post_archive
Create Date: 2026-10-12
"""
from alembic import op
import sqlalchemy as sa

revision = "021_scope_groups"
down_revision = "020_post_archive"
branch_labels = None
depends_on = None

TABLE = "scope_groups"
POSTS = "posts"
COLUMN = "group_id"
INDEX = "ix_posts_group_id"
FK_NAME = "fk_posts_group_id_scope_groups"


def _log(message: str) -> None:
    print(f"[021_scope_groups] {message}")


def upgrade() -> None:
    inspector = sa.inspect(op.get_bind())
    if TABLE not in inspector.get_table_names():
        _log(f"creating {TABLE}")
        op.create_table(
            TABLE,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("name", sa.String(length=255), nullable=False),
            sa.Column("parent_id", sa.Integer(), sa.ForeignKey("scope_groups.id"), nullable=True),
            sa.Column("parent_key", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("description", sa.String(length=1000), nullable=True),
            sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("parent_key", "name", name="uq_scope_groups_parent_name"),
        )
        op.create_index("ix_scope_groups_parent_id", TABLE, ["parent_id"])
        op.create_index("ix_scope_groups_is_deleted", TABLE, ["is_deleted"])

    inspector = sa.inspect(op.get_bind())
    if POSTS not in inspector.get_table_names():
        return
    if COLUMN not in {c["name"] for c in inspector.get_columns(POSTS)}:
        _log(f"adding column {POSTS}.{COLUMN}")
        with op.batch_alter_table(POSTS) as batch:
            batch.add_column(sa.Column(COLUMN, sa.Integer(), nullable=True))
            batch.create_foreign_key(FK_NAME, TABLE, [COLUMN], ["id"])
    if INDEX not in {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(POSTS)}:
        _log(f"creating index {INDEX}")
        op.create_index(INDEX, POSTS, [COLUMN])


def downgrade() -> None:
    """Drops posts.group_id and scope_groups: the tree is lost, every post goes back under the root.

    Order matters on MySQL: the foreign key needs the index of its column, so the constraint goes first, then the
    index, then the column, then the table the constraint pointed at.
    """
    inspector = sa.inspect(op.get_bind())
    if POSTS in inspector.get_table_names():
        fk_names = [
            fk["name"] for fk in inspector.get_foreign_keys(POSTS)
            if COLUMN in fk["constrained_columns"] and fk.get("name")
        ]
        if fk_names:
            with op.batch_alter_table(POSTS) as batch:
                for name in fk_names:
                    batch.drop_constraint(name, type_="foreignkey")
        if INDEX in {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(POSTS)}:
            op.drop_index(INDEX, table_name=POSTS)
        if COLUMN in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(POSTS)}:
            with op.batch_alter_table(POSTS) as batch:
                batch.drop_column(COLUMN)
    if TABLE in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table(TABLE)
