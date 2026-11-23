from pydantic import Field, ConfigDict
from typing import Iterable, List, Optional
from decimal import Decimal

from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.enums.general import ScopeGroupKind
from app.backend.models.observation import Observation
from app.backend.services.units import recorded_units
from app.backend.utils.validators import PlainTextModel, ResponseDatetime, ResponseModel
from app.backend.api.v1.models.response.posts import UnitRefResponse
from app.backend.api.v1.models.response.users import UserPublicResponse
from app.backend.api.v1.models.response.tags import TagBriefResponse


class ReportTypeChoice(ResponseModel):
    """One observation report type, for a select in the web UI"""
    value: str = Field(description="Value to send as report_type")
    label: str = Field(description="Ukrainian name")
    description: str = Field(description="Ukrainian description")


class MessageResponse(ResponseModel):  # TODO: i think we need make it global accessible to other routes and use it
    """Generic message response"""

    message: str = Field(description="Response message")


class ObservationFrequencyResponse(ResponseModel):
    """One frequency band of an observation (MHz)"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    observation_id: int
    kind: str
    link_group: Optional[int] = None
    frequency_min: Decimal
    frequency_max: Decimal
    frequency_center: Decimal
    bandwidth: Optional[Decimal] = None
    is_deleted: bool
    created_at: ResponseDatetime
    updated_at: ResponseDatetime


class ObservationWithMedia(PlainTextModel):
    """Observation with attached media file IDs"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    post_id: int
    signal_type_id: int
    observed_at: ResponseDatetime
    duration_seconds: Optional[int] = None
    comment: Optional[str] = None
    status: str
    confidence_level: str
    is_deleted: bool
    created_at: ResponseDatetime
    updated_at: ResponseDatetime
    disappeared_at: Optional[ResponseDatetime] = None
    disappeared_by: Optional[int] = None
    media_ids: List[int] = []
    tag_ids: List[int] = []
    # Active bands only; archived ones: GET /observations/{id}/frequencies?include_deleted=true
    frequencies: List[ObservationFrequencyResponse] = []

    # Nested projections named after the ORM relationships
    tags: List[TagBriefResponse] = []
    user: UserPublicResponse
    disappeared_by_user: Optional[UserPublicResponse] = None

    # Where the post was when the observation was created (observation_units), with today's names
    corps: Optional[UnitRefResponse] = Field(default=None, description="Corps of the post when the observation was created; null: outside any corps")
    brigade: Optional[UnitRefResponse] = Field(default=None, description="Brigade of the post when the observation was created; null: outside any brigade")


async def observations_to_response(session: AsyncSession, observations: Iterable[Observation]) -> List[ObservationWithMedia]:
    """Observation answers with their recorded units (one query for a page); the observations come with the
    relationships of OBSERVATION_LOAD_OPTIONS"""
    observations = list(observations)
    units = await recorded_units(session, (observation.id for observation in observations))

    def ref(observation_id: int, kind: ScopeGroupKind) -> Optional[UnitRefResponse]:
        unit = units.get(observation_id, {}).get(kind.value)
        return UnitRefResponse(id=unit[0], name=unit[1]) if unit else None

    return [
        ObservationWithMedia.model_validate(observation).model_copy(update={
            "corps": ref(observation.id, ScopeGroupKind.CORPS), "brigade": ref(observation.id, ScopeGroupKind.BRIGADE),
        })
        for observation in observations
    ]
