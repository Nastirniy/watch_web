from typing import Optional
from pydantic import Field

from app.backend.auth.context import AuthContext
from app.backend.models.scope_group import ScopeGroup
from app.backend.services.units import unit_counts
from app.backend.utils.validators import ResponseDatetime, SecureBaseModel


class CorpsResponse(SecureBaseModel):
    id: int
    name: str
    description: Optional[str] = None
    is_deleted: bool
    created_at: ResponseDatetime
    updated_at: ResponseDatetime
    brigade_count: int = Field(description="Active brigades of the corps the caller sees (scope_group.view)")
    post_count: int = Field(description="Active posts of the corps, its brigades included, where the caller has post.view")


def corps_to_response(corps: ScopeGroup, auth: AuthContext) -> CorpsResponse:
    """The counts come from the request's scope tree"""
    brigades, posts = unit_counts(auth, corps.id)
    return CorpsResponse(
        id=corps.id, name=corps.name, description=corps.description, is_deleted=corps.is_deleted,
        created_at=corps.created_at, updated_at=corps.updated_at, brigade_count=brigades, post_count=posts,
    )
