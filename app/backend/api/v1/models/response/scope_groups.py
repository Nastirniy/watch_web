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
    """The part of the scope tree the caller sees (scope_group.view, posts with post.view); archived groups and posts
    and everything below an archived group are left out"""
    groups: List[ScopeTreeGroup] = Field(
        description="The topmost groups the caller sees: the groups directly under the root with scope_group.view "
                    "at the root, otherwise the groups where it is held")
    posts: List[ScopeTreePost] = Field(
        description="The topmost posts the caller sees outside these groups: the posts directly under the root with "
                    "scope_group.view at the root, and posts where it is held on the post itself")
