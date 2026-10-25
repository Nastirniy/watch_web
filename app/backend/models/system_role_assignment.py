from typing import Optional
from datetime import datetime, timezone

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


class SystemRoleAssignment(SQLModel, table=True):
    """A system role held by a user (authorization v3): no node, it acts on the whole system. A tree role is never
    assigned here and a system role never in role_assignments (auth/system_assignments.py, auth/assignments.py).
    In evaluation it is a grant at the root of kind system (auth/context.py)."""
    __tablename__ = "system_role_assignments"
    __table_args__ = (UniqueConstraint("user_id", "role_id", name="uq_system_role_assignments_user_role"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    role_id: int = Field(foreign_key="roles.id", index=True)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # The user who made the assignment; null for the migration and seeding
    created_by: Optional[int] = Field(default=None)
