from typing import List, Optional
from pydantic import Field

from app.backend.utils.validators import ResponseDatetime, SecureBaseModel


class ScopeGroupResponse(SecureBaseModel):
    id: int
    name: str
    parent_id: Optional[int] = Field(default=None, description="Group it sits in; null: directly under the root")
    description: Optional[str] = None
    is_deleted: bool
    created_at: ResponseDatetime
    updated_at: ResponseDatetime


class ScopeTreePost(SecureBaseModel):
    id: int
    name: str
    code: str


class ScopeTreeGroup(SecureBaseModel):
    id: int
    name: str
    description: Optional[str] = None
    groups: List["ScopeTreeGroup"] = Field(description="Subgroups")
    posts: List[ScopeTreePost] = Field(description="Posts placed directly in this group")


class ScopeTreeResponse(SecureBaseModel):
    """The scope tree below the root; archived groups and posts are left out"""
    groups: List[ScopeTreeGroup]
    posts: List[ScopeTreePost] = Field(description="Posts directly under the root")
