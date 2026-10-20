"""
BrigadeFilterParams - Strongly typed filter parameters for brigades (GET /brigades)
"""

from typing import Optional
from pydantic import Field

from app.backend.api.v1.models.query.corps import UnitFilterParams


class BrigadeFilterParams(UnitFilterParams):
    """Filter parameters for brigade search"""

    corps_id: Optional[int] = Field(default=None, description="Brigades of this corps")
