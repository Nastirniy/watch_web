from typing import FrozenSet, Iterable, List, Optional
from pydantic import Field

from app.backend.api.v1.models.response.posts import ObservationStatsFigures
from app.backend.auth.context import AuthContext
from app.backend.enums.general import Permissions, UnitPlacement
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


def _corps_response(corps: ScopeGroup, auth: AuthContext, viewable: Optional[FrozenSet[int]]) -> CorpsResponse:
    brigades, posts = unit_counts(auth, corps.id, viewable)
    return CorpsResponse(
        id=corps.id, name=corps.name, description=corps.description, is_deleted=corps.is_deleted,
        created_at=corps.created_at, updated_at=corps.updated_at, brigade_count=brigades, post_count=posts,
    )


def corps_to_response(corps: ScopeGroup, auth: AuthContext) -> CorpsResponse:
    """The counts come from the request's scope tree"""
    return _corps_response(corps, auth, auth.posts_with(Permissions.POST_VIEW))


def corps_list_to_response(rows: Iterable[ScopeGroup], auth: AuthContext) -> List[CorpsResponse]:
    """corps_to_response of each row, with the post.view scope of the caller read once for the list"""
    viewable = auth.posts_with(Permissions.POST_VIEW)
    return [_corps_response(corps, auth, viewable) for corps in rows]


class UnitObservationStatsResponse(ObservationStatsFigures):
    """Observation counters of a corps or brigade (GET /corps/{id}/observation-stats, /brigades/{id}/...)"""
    unit_id: int
    placement: UnitPlacement = Field(description="Which observations belong to the unit: recorded (the unit stored "
                                                 "when the observation was created) or current (its post is in the "
                                                 "unit now)")
