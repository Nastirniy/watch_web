"""
Search response models for the unified search API
"""

from typing import Generic, List, Sequence, TypeVar

from app.backend.utils.validators import PlainTextModel

Item = TypeVar("Item")


class SearchResponse(PlainTextModel, Generic[Item]):
    """One page of a search: the items are the same response model the list route of the entity returns
    (SearchResponse[UserResponse], ...), never raw table rows"""
    entity: str
    items: List[Item]
    total: int
    page: int
    page_size: int
    total_pages: int

    model_config = {
        "extra": "forbid",
    }

    @classmethod
    def create(cls, entity: str, items: Sequence[Item], total: int, page: int, page_size: int) -> "SearchResponse[Item]":
        """Page with total_pages computed"""
        total_pages = (total + page_size - 1) // page_size if page_size > 0 else 0
        return cls(entity=entity, items=list(items), total=total, page=page, page_size=page_size, total_pages=total_pages)
