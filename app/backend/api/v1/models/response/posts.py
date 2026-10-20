from typing import Dict, List, Mapping, Optional
from pydantic import BaseModel, ConfigDict, Field
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.auth.scope import ScopeTree
from app.backend.enums.general import ScopeGroupKind
from app.backend.models.post import Post
from app.backend.services.units import post_unit_names, post_units
from app.backend.utils.validators import PlainTextModel, ResponseDatetime


class UnitRefResponse(PlainTextModel):
    """A corps or brigade: its id and today's name"""
    id: int
    name: str


class PostResponse(PlainTextModel):
    """Post row plus presence of its home users (users whose users.post_id is the post)"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    code: str
    description: Optional[str] = None
    region: Optional[str] = None
    city: Optional[str] = None
    is_active: bool
    group_id: Optional[int] = Field(default=None, description="Scope group the post sits in; null: directly under the root")
    corps: Optional[UnitRefResponse] = Field(default=None, description="Corps the post sits in now (directly or through a brigade); null: outside any corps")
    brigade: Optional[UnitRefResponse] = Field(default=None, description="Brigade the post sits in now; null: outside any brigade")
    is_deleted: bool = Field(default=False, description="Archived (POST /posts/{post_id}/archive): its data is read-only")
    online_user_count: int = Field(default=0, description="Home users with an authenticated request within security.online_threshold_seconds")
    last_user_activity_at: Optional[ResponseDatetime] = Field(default=None, description="Latest activity of any home user; null when none has an active session")
    user_count: int = Field(default=0, description="Active, not archived users whose home post is this post")
    created_at: ResponseDatetime
    updated_at: ResponseDatetime


def _unit(units: Dict[str, int], kind: ScopeGroupKind, names: Mapping[int, str]) -> Optional[UnitRefResponse]:
    unit_id = units.get(kind.value)
    if unit_id is None or unit_id not in names:
        return None
    return UnitRefResponse(id=unit_id, name=names[unit_id])


def post_to_response(post: Post, tree: ScopeTree, names: Mapping[int, str]) -> PostResponse:
    """tree: the request's scope tree; names: the unit names of the page (services/units.py post_unit_names)"""
    units = post_units(tree, post.group_id)
    return PostResponse(
        id=post.id,
        name=post.name,
        code=post.code,
        description=post.description,
        region=post.region,
        city=post.city,
        is_active=post.is_active,
        group_id=post.group_id,
        corps=_unit(units, ScopeGroupKind.CORPS, names),
        brigade=_unit(units, ScopeGroupKind.BRIGADE, names),
        is_deleted=post.is_deleted,
        online_user_count=post.online_user_count or 0,
        last_user_activity_at=post.last_user_activity_at,
        user_count=post.user_count or 0,
        created_at=post.created_at,
        updated_at=post.updated_at,
    )


async def posts_to_response(session: AsyncSession, tree: ScopeTree, posts: List[Post]) -> List[PostResponse]:
    """Post answers with their units (one query for the unit names of the page)"""
    names = await post_unit_names(session, tree, [post.group_id for post in posts])
    return [post_to_response(post, tree, names) for post in posts]



class ObservationCount(BaseModel):
    """Observations by created_at. The two figures never overlap: all = not_archived + archived."""
    not_archived: int = Field(description="Observations that are not archived")
    archived: Optional[int] = Field(default=None, description="Archived observations; null unless include_archived")


class ObservationStatsBucket(ObservationCount):
    start: ResponseDatetime = Field(description="Bucket start in the requested time zone")
    end: ResponseDatetime


class ObservationStatsPeriod(ObservationCount):
    date_from: ResponseDatetime
    date_to: ResponseDatetime
    group_by: str
    tz: str
    buckets: List[ObservationStatsBucket] = Field(description="Every bucket of the period in order, empty ones included")


class ObservationStatsFigures(BaseModel):
    """The counters of a set of observations (a post, a corps, a brigade)"""
    generated_at: ResponseDatetime
    last_observation_at: Optional[ResponseDatetime] = Field(default=None, description="created_at of the latest not archived observation; null when there is none")
    days_without_observations: Optional[int] = Field(
        default=None,
        description="Calendar days in tz from the date of last_observation_at to the date of generated_at: 0 today, 1 yesterday; null when there is no observation",
    )
    all_time: ObservationCount
    last_24h: ObservationCount = Field(description="Created within the 24 hours before generated_at")
    period: ObservationStatsPeriod


class PostObservationStatsResponse(ObservationStatsFigures):
    post_id: int
