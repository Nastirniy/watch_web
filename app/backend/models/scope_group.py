from typing import Optional
from datetime import datetime, timezone
from sqlalchemy import UniqueConstraint, false
from sqlmodel import Field, SQLModel


class ScopeGroup(SQLModel, table=True):
    """A node of the scope tree between the root and the posts: a group holds groups and posts (/scope-groups).

    parent_key is parent_id or 0, written by the code: the unique name per parent also holds for root groups
    (a NULL parent_id would not collide in a unique index).
    """
    __tablename__ = "scope_groups"
    __table_args__ = (UniqueConstraint("parent_key", "name", name="uq_scope_groups_parent_name"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(max_length=255)
    parent_id: Optional[int] = Field(default=None, foreign_key="scope_groups.id", index=True)
    parent_key: int = Field(default=0)
    description: Optional[str] = Field(default=None, max_length=1000)
    is_deleted: bool = Field(default=False, index=True, sa_column_kwargs={"server_default": false(), "nullable": False})
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
