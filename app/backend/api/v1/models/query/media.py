"""
MediaFilterParams - Strongly typed filter parameters for MediaFile
"""

from typing import Optional, Literal
from pydantic import Field, field_validator

from app.backend.api.v1.models.query.base import BaseFilterParams
from app.backend.enums.general import MediaType, UnitPlacement
from app.backend.utils.validators import UtcDatetime

# Fields the results can be sorted by; sort_by accepts exactly these (another value answers 422)
MediaSortField = Literal["id", "original_filename", "file_size", "media_type", "uploaded_at", "observation_id"]


class MediaFilterParams(BaseFilterParams):
    """Filter parameters for MediaFile search"""

    sort_by: Optional[MediaSortField] = Field(default=None, description="Field to sort by")

    id: Optional[int] = Field(default=None, description="Exact media ID")
    observation_id: Optional[int] = Field(default=None, description="Filter by observation ID")
    corps_id: Optional[int] = Field(default=None, description="Media of observations of this corps, its brigades included (see placement); an id of another kind gives nothing")
    brigade_id: Optional[int] = Field(default=None, description="Media of observations of this brigade (see placement); an id of another kind gives nothing")
    placement: UnitPlacement = Field(default=UnitPlacement.RECORDED, description="Which unit corps_id / brigade_id match: recorded (where the post was when the observation was created) or current (where the post is now, archived posts left out). Not a time filter")
    original_filename_contains: Optional[str] = Field(default=None, max_length=100, description="Filename contains text")
    media_type: Optional[MediaType] = Field(default=None, description="Media type")
    mime_type: Optional[str] = Field(default=None, description="Exact MIME type")
    file_extension: Optional[str] = Field(default=None, max_length=11, description="File extension, with or without the dot, any case: png, .png, PNG")
    file_size_from: Optional[int] = Field(default=None, ge=0, description="File size min (bytes)")
    file_size_to: Optional[int] = Field(default=None, ge=0, description="File size max (bytes)")
    width_from: Optional[int] = Field(default=None, ge=0, description="Width min (pixels)")
    width_to: Optional[int] = Field(default=None, ge=0, description="Width max (pixels)")
    height_from: Optional[int] = Field(default=None, ge=0, description="Height min (pixels)")
    height_to: Optional[int] = Field(default=None, ge=0, description="Height max (pixels)")
    has_thumbnail: Optional[bool] = Field(default=None, description="Has a thumbnail available (images always, videos once the poster is ready)")
    uploaded_at_from: Optional[UtcDatetime] = Field(default=None, description="Uploaded after")
    uploaded_at_to: Optional[UtcDatetime] = Field(default=None, description="Uploaded before")
    include_deleted: bool = Field(default=False, description="Include archived media (requires media.archive held anywhere; archived media only at the posts where it is in effect); media of archived observations are never listed")

    @field_validator("file_extension")
    @classmethod
    def stored_extension(cls, value: Optional[str]) -> Optional[str]:
        """Files keep their extension as .ext in lower case"""
        if value is None or not value.strip(".").strip():
            return None
        return "." + value.strip().lstrip(".").lower()
