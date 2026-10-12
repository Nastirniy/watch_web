from typing import Optional, TYPE_CHECKING
from datetime import datetime, timezone
from sqlalchemy import CheckConstraint, UniqueConstraint
from sqlmodel import Field, SQLModel, Relationship

from app.backend.auth.scope import Node

if TYPE_CHECKING:
    from app.backend.models.role import Role
    from app.backend.models.user import User


def scope_columns(node: Node) -> dict:
    """The three scope fields of an assignment at the node: both ids null at the root, never both set"""
    return {
        "scope_group_id": node.id if node.kind == "group" else None,
        "scope_post_id": node.id if node.kind == "post" else None,
        "scope_key": node.key,
    }


def node_of(scope_group_id: Optional[int], scope_post_id: Optional[int]) -> Node:
    """The node of an assignment's scope columns"""
    if scope_post_id is not None:
        return Node.post(scope_post_id)
    if scope_group_id is not None:
        return Node.group(scope_group_id)
    return Node.root()


class RoleAssignment(SQLModel, table=True):
    """A role held at one node of the scope tree (authorization v2): the root (both scope ids null), a scope group or a
    post. It applies to the node and everything below it. scope_key ("root", "g:<id>", "p:<id>", written by the code
    from the scope ids, scope_columns) makes one role at one node unique per user, a NULL id included."""
    __tablename__ = "role_assignments"
    __table_args__ = (
        UniqueConstraint("user_id", "role_id", "scope_key", name="uq_role_assignments_user_role_scope"),
        CheckConstraint("scope_group_id IS NULL OR scope_post_id IS NULL", name="ck_role_assignments_one_scope"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    role_id: int = Field(foreign_key="roles.id", index=True)
    scope_group_id: Optional[int] = Field(default=None, foreign_key="scope_groups.id", index=True)
    scope_post_id: Optional[int] = Field(default=None, foreign_key="posts.id", index=True)
    scope_key: str = Field(max_length=24)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # The user who made the assignment; null for the migration and seeding
    created_by: Optional[int] = Field(default=None)

    # Relationships
    user: "User" = Relationship(back_populates="assignments")
    role: "Role" = Relationship(back_populates="assignments")

    @property
    def node(self) -> Node:
        return node_of(self.scope_group_id, self.scope_post_id)
