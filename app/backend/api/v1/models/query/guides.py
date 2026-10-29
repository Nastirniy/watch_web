"""GuideFilterParams - the query of the guide list (criteria: services/guides.py GuideCriteria)"""

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.backend.api.v1.models.query.base import BaseFilterParams

# Fields the list can be sorted by; the other one follows, then the id (sort_order without sort_by)
GuideSortField = Literal["sort_order", "title"]

INCLUDE_DELETED = "Archived guides and archived files too (requires guide.archive)"


class GuideFilterParams(BaseFilterParams):
    """Filter parameters of the guide list"""

    sort_by: Optional[GuideSortField] = Field(
        default=None, description="Field to sort by first (sort_order when left out); the other one follows")
    q: Optional[str] = Field(default=None, max_length=200, description="Title contains text (case-insensitive)")
    include_deleted: bool = Field(default=False, description=INCLUDE_DELETED)


class GuideReadParams(BaseModel):
    """The query of GET /guides/{guide_id}"""

    model_config = ConfigDict(extra="forbid")

    include_deleted: bool = Field(default=False, description=INCLUDE_DELETED)
