from pydantic import BaseModel, Field, ConfigDict
from typing import List, Optional
from datetime import datetime
from decimal import Decimal

from app.backend.utils.validators import PlainTextModel
from app.backend.routes.models.response.user_response import UserPublicResponse
from app.backend.routes.models.response.tag_response import TagBriefResponse


class ReportTypeChoice(BaseModel):
    """One observation report type, for a select in the web UI"""
    value: str = Field(description="Value to send as report_type")
    label: str = Field(description="Ukrainian name")
    description: str = Field(description="Ukrainian description")


class MessageResponse(BaseModel):  # TODO: i think we need make it global accessible to other routes and use it
    """Generic message response"""

    message: str = Field(description="Response message")


class ObservationFrequencyResponse(BaseModel):
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
    created_at: datetime
    updated_at: datetime


class ObservationWithMedia(PlainTextModel):
    """Observation with attached media file IDs"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    post_id: int
    signal_type_id: int
    observed_at: datetime
    duration_seconds: Optional[int] = None
    comment: Optional[str] = None
    status: str
    confidence_level: str
    is_deleted: bool
    created_at: datetime
    updated_at: datetime
    disappeared_at: Optional[datetime] = None
    disappeared_by: Optional[int] = None
    media_ids: List[int] = []
    tag_ids: List[int] = []
    # Active bands only; archived ones: GET /observations/{id}/frequencies?include_deleted=true
    frequencies: List[ObservationFrequencyResponse] = []

    # Nested projections named after the ORM relationships
    tags: List[TagBriefResponse] = []
    user: UserPublicResponse
    disappeared_by_user: Optional[UserPublicResponse] = None
