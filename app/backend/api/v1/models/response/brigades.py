from typing import FrozenSet, Iterable, List, Optional
from pydantic import Field

from app.backend.auth.context import AuthContext
from app.backend.enums.general import Permissions
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


class BrigadeUpdateResponse(BrigadeResponse):
    """The brigade after PATCH /brigades/{brigade_id}"""
    already_there: bool = Field(default=False, description="true: the move this request asked for has already happened: nothing was written, the other fields sent included, and this is the record as it stands")


def _brigade_response(brigade: ScopeGroup, auth: AuthContext, viewable: Optional[FrozenSet[int]]) -> BrigadeResponse:
    _, posts = unit_counts(auth, brigade.id, viewable)
    return BrigadeResponse(
        id=brigade.id, corps_id=brigade.parent_id, name=brigade.name, description=brigade.description,
        is_deleted=brigade.is_deleted, created_at=brigade.created_at, updated_at=brigade.updated_at, post_count=posts,
    )


def brigade_to_response(brigade: ScopeGroup, auth: AuthContext) -> BrigadeResponse:
    """The count comes from the request's scope tree"""
    return _brigade_response(brigade, auth, auth.posts_with(Permissions.POST_VIEW))


def brigade_list_to_response(rows: Iterable[ScopeGroup], auth: AuthContext) -> List[BrigadeResponse]:
    """brigade_to_response of each row, with the post.view scope of the caller read once for the list"""
    viewable = auth.posts_with(Permissions.POST_VIEW)
    return [_brigade_response(brigade, auth, viewable) for brigade in rows]
