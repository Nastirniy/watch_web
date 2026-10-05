"""
SimpleFilterEngine - Applies the criteria of a filter service to queries
Every service declares its criteria as a frozen dataclass extending ListCriteria
"""

from dataclasses import dataclass
from typing import Optional, Type, TypeVar, List, Set
from datetime import datetime, timezone, timedelta
from sqlalchemy.sql.selectable import Select

from sqlalchemy import inspect as sa_inspect
from sqlalchemy.exc import NoInspectionAvailable

from app.backend.enums.general import SortOrder

C = TypeVar("C")


@dataclass(frozen=True, kw_only=True)
class ListCriteria:
    """Pagination and sorting of a list; every filter service extends it with what it filters by"""
    page: int = 1
    page_size: int = 50
    sort_by: Optional[str] = None
    sort_order: SortOrder = SortOrder.ASCENDING

    @property
    def offset(self) -> int:
        return (self.page - 1) * self.page_size


def as_criteria(criteria_class: Type[C], params) -> C:
    """The criteria of a service from the query model of an API version (the same field names). A field the
    service does not know is a TypeError, never a silently ignored filter; a version whose names differ
    builds the criteria itself."""
    if isinstance(params, criteria_class):
        return params
    return criteria_class(**params.model_dump())


def _get_model_columns(model: Type) -> Set[str]:
    """Get all column names from a SQLModel class"""
    if model is None:
        return set()
    if hasattr(model, '__table__'):
        columns = {c.name for c in model.__table__.columns}
        # Mapper-level column properties (e.g. presence values attached in models/presence_properties.py)
        try:
            columns |= {attr.key for attr in sa_inspect(model).column_attrs}
        except NoInspectionAvailable:
            pass
        return columns
    if hasattr(model, 'model_fields'):
        return set(model.model_fields.keys())
    return set()


class SimpleFilterEngine:
    """
    Base engine for applying simple filter params to SQLModel queries.
    Subclasses implement apply_filters() for each entity.

    SORTABLE_FIELDS are validated against MODEL columns at import time.
    If a field listed in SORTABLE_FIELDS does not exist on MODEL,
    an AttributeError is raised immediately.

    CRITERIA - the criteria dataclass of this service; criteria() converts a query model into it.
    EXPRESSION_SORTABLE - entries of SORTABLE_FIELDS that are not MODEL columns; the
    subclass builds their ORDER BY expression in _sort_expression().
    """

    MODEL: Type = None
    CRITERIA: Type[ListCriteria] = ListCriteria
    SORTABLE_FIELDS: List[str] = []
    EXPRESSION_SORTABLE: List[str] = []

    def __init_subclass__(cls, **kwargs):
        """Validate SORTABLE_FIELDS against MODEL when subclass is defined"""
        super().__init_subclass__(**kwargs)
        if cls.MODEL is not None and cls.SORTABLE_FIELDS:
            model_columns = _get_model_columns(cls.MODEL)
            if model_columns:
                invalid = [f for f in cls.SORTABLE_FIELDS if f not in model_columns and f not in cls.EXPRESSION_SORTABLE]
                if invalid:
                    raise AttributeError(
                        f"{cls.__name__}.SORTABLE_FIELDS contains fields "
                        f"not found on {cls.MODEL.__name__}: {invalid}. "
                        f"Available columns: {sorted(model_columns)}"
                    )

    @classmethod
    def criteria(cls, params) -> ListCriteria:
        """The criteria of this service from a query model (see as_criteria)"""
        return as_criteria(cls.CRITERIA, params)

    @classmethod
    def _apply_exact(cls, query: Select, column, value) -> Select:
        """Apply exact match filter"""
        if value is not None:
            return query.where(column == value)
        return query

    @classmethod
    def _apply_contains(cls, query: Select, column, value: Optional[str]) -> Select:
        """Apply case-insensitive contains filter"""
        if value:
            return query.where(column.ilike(f"%{value}%"))
        return query

    @classmethod
    def _apply_in_list(cls, query: Select, column, values: Optional[List]) -> Select:
        """Apply IN list filter"""
        if values:
            return query.where(column.in_(values))
        return query

    @classmethod
    def _apply_range(cls, query: Select, column, from_val, to_val) -> Select:
        """Apply range filter (from/to)"""
        if from_val is not None:
            query = query.where(column >= from_val)
        if to_val is not None:
            query = query.where(column <= to_val)
        return query

    @classmethod
    def _apply_is_null(cls, query: Select, column, is_null: Optional[bool]) -> Select:
        """Apply null check filter"""
        if is_null is True:
            return query.where(column.is_(None))
        elif is_null is False:
            return query.where(column.isnot(None))
        return query

    @classmethod
    def _apply_pagination(cls, query: Select, params: ListCriteria) -> Select:
        """Apply pagination"""
        return query.offset(params.offset).limit(params.page_size)

    @classmethod
    def _sort_expression(cls, name: str, params: ListCriteria):
        """ORDER BY target for a sortable field: a MODEL column; override for EXPRESSION_SORTABLE entries"""
        return getattr(cls.MODEL, name, None)

    @classmethod
    def _apply_sorting(cls, query: Select, params: ListCriteria) -> Select:
        """Apply sorting using SortOrder enum"""
        if params.sort_by and params.sort_by in cls.SORTABLE_FIELDS:
            column = cls._sort_expression(params.sort_by, params)
            if column is not None:
                order = params.sort_order
                if order == SortOrder.ASCENDING:
                    query = query.order_by(column.asc())
                elif order == SortOrder.DESCENDING:
                    query = query.order_by(column.desc())
                elif order == SortOrder.ASCENDING_NULLS_FIRST:
                    query = query.order_by(column.asc().nulls_first())
                elif order == SortOrder.ASCENDING_NULLS_LAST:
                    query = query.order_by(column.asc().nulls_last())
                elif order == SortOrder.DESCENDING_NULLS_FIRST:
                    query = query.order_by(column.desc().nulls_first())
                elif order == SortOrder.DESCENDING_NULLS_LAST:
                    query = query.order_by(column.desc().nulls_last())
        return query

    @classmethod
    def _apply_soft_delete(cls, query: Select, params: ListCriteria) -> Select:
        """Apply soft delete filter if model has is_deleted"""
        include_deleted = getattr(params, 'include_deleted', False)
        if hasattr(cls.MODEL, 'is_deleted') and not include_deleted:
            query = query.where(cls.MODEL.is_deleted == False)
        return query

    @classmethod
    def _get_last_n_days_start(cls, n_days: int) -> datetime:
        """Get start datetime for last N days filter"""
        now = datetime.now(timezone.utc)
        target_date = now.date() - timedelta(days=n_days - 1)
        return datetime.combine(target_date, datetime.min.time()).replace(tzinfo=timezone.utc)
