"""
PostFilterService - Filter service for Post entity
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional, Iterable
from sqlalchemy.sql.selectable import Select

from app.backend.models.post import Post
from app.backend.filters.simple_engine import ListCriteria, SimpleFilterEngine


@dataclass(frozen=True, kw_only=True)
class PostCriteria(ListCriteria):
    """What PostFilterService filters by; each API version converts its query model into it"""
    id: Optional[int] = None
    name: Optional[str] = None
    name_contains: Optional[str] = None
    code: Optional[str] = None
    code_contains: Optional[str] = None
    region: Optional[str] = None
    region_contains: Optional[str] = None
    city: Optional[str] = None
    city_contains: Optional[str] = None
    description_contains: Optional[str] = None
    is_active: Optional[bool] = None
    has_online_users: Optional[bool] = None
    created_at_from: Optional[datetime] = None
    created_at_to: Optional[datetime] = None
    last_user_activity_from: Optional[datetime] = None
    last_user_activity_to: Optional[datetime] = None


class PostFilterService(SimpleFilterEngine):
    """Filter service for Post entity"""

    MODEL = Post
    CRITERIA = PostCriteria
    SORTABLE_FIELDS = ["id", "name", "code", "city", "created_at", "updated_at", "last_user_activity_at", "is_active", "online_user_count", "user_count"]

    @classmethod
    def _apply_has_online_users(cls, query: Select, value: Optional[bool]) -> Select:
        """Posts with (true) or without (false) a home user online now"""
        if value is None:
            return query
        return query.where(Post.online_user_count > 0 if value else Post.online_user_count == 0)

    @classmethod
    def apply_filters(
        cls,
        query: Select,
        params: PostCriteria,
        access_post_ids: Optional[Iterable[int]] = None
    ) -> Select:
        """Apply all filters from PostCriteria"""
        params = cls.criteria(params)

        # Post has no is_deleted field, skip soft delete

        # Access control (user can only see their post)
        if access_post_ids is not None:
            query = query.where(Post.id.in_(list(access_post_ids)))

        # ID filter
        query = cls._apply_exact(query, Post.id, params.id)

        # Name filters
        query = cls._apply_exact(query, Post.name, params.name)
        query = cls._apply_contains(query, Post.name, params.name_contains)

        # Code filters
        query = cls._apply_exact(query, Post.code, params.code)
        query = cls._apply_contains(query, Post.code, params.code_contains)

        # Region filters
        query = cls._apply_exact(query, Post.region, params.region)
        query = cls._apply_contains(query, Post.region, params.region_contains)

        # City filters
        query = cls._apply_exact(query, Post.city, params.city)
        query = cls._apply_contains(query, Post.city, params.city_contains)

        # Description search
        query = cls._apply_contains(query, Post.description, params.description_contains)

        # Status filters
        query = cls._apply_exact(query, Post.is_active, params.is_active)
        query = cls._apply_has_online_users(query, params.has_online_users)

        # Date ranges
        query = cls._apply_range(query, Post.created_at, params.created_at_from, params.created_at_to)
        query = cls._apply_range(query, Post.last_user_activity_at, params.last_user_activity_from, params.last_user_activity_to)

        # Sorting & pagination
        query = cls._apply_sorting(query, params)
        query = cls._apply_pagination(query, params)

        return query

    @classmethod
    def apply_filters_for_count(
        cls,
        query: Select,
        params: PostCriteria,
        access_post_ids: Optional[Iterable[int]] = None
    ) -> Select:
        """Apply filters without pagination"""
        params = cls.criteria(params)
        if access_post_ids is not None:
            query = query.where(Post.id.in_(list(access_post_ids)))
        query = cls._apply_exact(query, Post.id, params.id)
        query = cls._apply_exact(query, Post.name, params.name)
        query = cls._apply_contains(query, Post.name, params.name_contains)
        query = cls._apply_exact(query, Post.code, params.code)
        query = cls._apply_contains(query, Post.code, params.code_contains)
        query = cls._apply_exact(query, Post.region, params.region)
        query = cls._apply_contains(query, Post.region, params.region_contains)
        query = cls._apply_exact(query, Post.city, params.city)
        query = cls._apply_contains(query, Post.city, params.city_contains)
        query = cls._apply_contains(query, Post.description, params.description_contains)
        query = cls._apply_exact(query, Post.is_active, params.is_active)
        query = cls._apply_has_online_users(query, params.has_online_users)
        query = cls._apply_range(query, Post.created_at, params.created_at_from, params.created_at_to)
        query = cls._apply_range(query, Post.last_user_activity_at, params.last_user_activity_from, params.last_user_activity_to)
        return query
