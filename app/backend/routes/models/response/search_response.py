"""
Search response models for the unified search API
"""

from typing import List, Any, Dict
from pydantic import BaseModel


class SearchResponse(BaseModel):
    """Response model for search endpoint"""
    entity: str
    items: List[Dict[str, Any]]
    total: int
    page: int
    page_size: int
    total_pages: int

    model_config = {
        "extra": "forbid",
        "from_attributes": True
    }

    @classmethod
    def create(
        cls,
        entity: str,
        items: List[Any],
        total: int,
        page: int,
        page_size: int
    ) -> "SearchResponse":
        """Factory method to create SearchResponse with computed total_pages"""
        total_pages = (total + page_size - 1) // page_size if page_size > 0 else 0
        
        processed_items = []
        for item in items:
            if isinstance(item, dict):
                processed_items.append(item)
            elif hasattr(item, 'model_dump'):
                # Prefer model_dump() for compatibility, but if it's an ORM object 
                # that we want to validate through a response model, we already 
                # handle that in the route. Here we just ensure it's a dict.
                processed_items.append(item.model_dump())
            else:
                processed_items.append(dict(item))

        return cls(
            entity=entity,
            items=processed_items,
            total=total,
            page=page,
            page_size=page_size,
            total_pages=total_pages
        )


class FilterParamInfo(BaseModel):
    """Single filter parameter description"""
    name: str
    type: str
    description: str


class EntitySchemaInfo(BaseModel):
    """Schema info for a single entity"""
    sortable_fields: List[str]
    filter_params: List[FilterParamInfo]


class SchemaResponse(BaseModel):
    """Response for /schema endpoint - metadata about all searchable entities"""
    entities: Dict[str, EntitySchemaInfo]
    common_params: List[FilterParamInfo]
