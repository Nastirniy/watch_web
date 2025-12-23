from pydantic import BaseModel, Field
from typing import List, Optional
from datetime import datetime
from decimal import Decimal


class MessageResponse(BaseModel):  # TODO: i think we need make it global accessible to other routes and use it
    """Generic message response"""

    message: str = Field(description="Response message")


class ObservationWithMedia(BaseModel):
    """Observation with attached media file IDs"""
    id: int
    user_id: int
    post_id: int
    signal_type_id: int
    frequency_min: Decimal
    frequency_max: Decimal
    frequency_center: Optional[Decimal] = None
    bandwidth: Optional[Decimal] = None
    video_frequency_min: Optional[Decimal] = None
    video_frequency_max: Optional[Decimal] = None
    video_frequency_center: Optional[Decimal] = None
    video_bandwidth: Optional[Decimal] = None
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
