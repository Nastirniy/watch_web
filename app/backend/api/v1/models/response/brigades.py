from typing import Optional
from pydantic import Field

from app.backend.auth.context import AuthContext
from app.backend.models.scope_group import ScopeGroup
from app.backend.services.units import unit_counts
from app.backend.utils.validators import ResponseDatetime, SecureBaseModel


class BrigadeResponse(SecureBaseModel):
    id: int
    corps_id: int = Field(description="Corps the brigade sits in")
    name: str
    description: Optional[str] = None
    is_deleted: bool
    created_at: ResponseDatetime
    updated_at: ResponseDatetime
    post_count: int = Field(description="Active posts of the brigade where the caller has post.view")


def brigade_to_response(brigade: ScopeGroup, auth: AuthContext) -> BrigadeResponse:
    """The count comes from the request's scope tree"""
    _, posts = unit_counts(auth, brigade.id)
    return BrigadeResponse(
        id=brigade.id, corps_id=brigade.parent_id, name=brigade.name, description=brigade.description,
        is_deleted=brigade.is_deleted, created_at=brigade.created_at, updated_at=brigade.updated_at, post_count=posts,
    )
