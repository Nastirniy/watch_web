"""
SimpleFilterEngine - Applies simple typed filter params to queries
Works with the strongly-typed FilterParams classes
"""

from typing import Optional, Type, List, Set, Dict, Any, Union, get_origin, get_args
from datetime import datetime, timezone, timedelta
from sqlalchemy import and_, or_
from sqlalchemy.sql.selectable import Select
from sqlmodel import select
from loguru import logger

from app.backend.filters.params.base_params import BaseFilterParams, SortOrder

# Common base param field names (pagination/sorting - same for all entities)
_BASE_COMMON_FIELDS = {"page", "page_size", "sort_by", "sort_order"}


def _get_type_name(annotation) -> str:
    """Convert Python type annotation to simple type name for schema"""
    origin = get_origin(annotation)
    if origin is Union:
        args = [a for a in get_args(annotation) if a is not type(None)]
        if args:
            return _get_type_name(args[0])

    _type_map = {
        int: "integer",
        str: "string",
        float: "number",
        bool: "boolean",
    }
    if annotation in _type_map:
        return _type_map[annotation]

    type_name = getattr(annotation, "__name__", str(annotation))
    _name_map = {
        "Decimal": "number",
        "datetime": "datetime",
    }
    return _name_map.get(type_name, type_name)


def _get_model_columns(model: Type) -> Set[str]:
    """Get all column names from a SQLModel class"""
    if model is None:
        return set()
    if hasattr(model, '__table__'):
        columns = {c.name for c in model.__table__.columns}
        # Mapper-level column properties (e.g. presence values attached in models/presence_properties.py)
        try:
            from sqlalchemy import inspect as sa_inspect
            columns |= {attr.key for attr in sa_inspect(model).column_attrs}
        except Exception:
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

    ADMIN_ONLY_SORTABLE - sortable fields hidden from non-admin users in schema.
    ADMIN_ONLY_PARAMS - filter params hidden from non-admin users in schema.
    PARAMS - the FilterParams class used by this service (for schema introspection).
    """

    MODEL: Type = None
    PARAMS: Type = None
    SORTABLE_FIELDS: List[str] = []
    ADMIN_ONLY_SORTABLE: List[str] = []
    ADMIN_ONLY_PARAMS: List[str] = []

    def __init_subclass__(cls, **kwargs):
        """Validate SORTABLE_FIELDS and ADMIN_ONLY_* against MODEL/PARAMS when subclass is defined"""
        super().__init_subclass__(**kwargs)
        if cls.MODEL is not None and cls.SORTABLE_FIELDS:
            model_columns = _get_model_columns(cls.MODEL)
            if model_columns:
                invalid = [f for f in cls.SORTABLE_FIELDS if f not in model_columns]
                if invalid:
                    raise AttributeError(
                        f"{cls.__name__}.SORTABLE_FIELDS contains fields "
                        f"not found on {cls.MODEL.__name__}: {invalid}. "
                        f"Available columns: {sorted(model_columns)}"
                    )

        if cls.ADMIN_ONLY_SORTABLE:
            invalid_admin = [f for f in cls.ADMIN_ONLY_SORTABLE if f not in cls.SORTABLE_FIELDS]
            if invalid_admin:
                raise AttributeError(
                    f"{cls.__name__}.ADMIN_ONLY_SORTABLE contains fields "
                    f"not in SORTABLE_FIELDS: {invalid_admin}. "
                    f"Available: {cls.SORTABLE_FIELDS}"
                )

        if cls.PARAMS is not None and cls.ADMIN_ONLY_PARAMS:
            param_fields = set(cls.PARAMS.model_fields.keys())
            invalid_params = [f for f in cls.ADMIN_ONLY_PARAMS if f not in param_fields]
            if invalid_params:
                raise AttributeError(
                    f"{cls.__name__}.ADMIN_ONLY_PARAMS contains fields "
                    f"not found on {cls.PARAMS.__name__}: {invalid_params}. "
                    f"Available params: {sorted(param_fields)}"
                )

    @classmethod
    def get_sortable_fields(cls, is_admin: bool = True) -> List[str]:
        """Get sortable fields filtered by access level"""
        if is_admin:
            return list(cls.SORTABLE_FIELDS)
        return [f for f in cls.SORTABLE_FIELDS if f not in cls.ADMIN_ONLY_SORTABLE]

    @classmethod
    def get_filter_param_names(cls, is_admin: bool = True) -> List[str]:
        """Get filter param field names filtered by access level"""
        if cls.PARAMS is None:
            return []
        all_fields = list(cls.PARAMS.model_fields.keys())
        if is_admin:
            return all_fields
        return [f for f in all_fields if f not in cls.ADMIN_ONLY_PARAMS]

    @classmethod
    def get_schema(cls, is_admin: bool = True) -> Dict[str, Any]:
        """Return schema metadata: sortable fields and filter params for this entity.
        Returns a plain dict to avoid coupling with response models."""
        sortable = cls.get_sortable_fields(is_admin=is_admin)

        param_names = cls.get_filter_param_names(is_admin=is_admin)
        entity_params = [n for n in param_names if n not in _BASE_COMMON_FIELDS]

        filter_params = []
        if cls.PARAMS is not None:
            for name in entity_params:
                field_info = cls.PARAMS.model_fields.get(name)
                if field_info is None:
                    continue
                filter_params.append({
                    "name": name,
                    "type": _get_type_name(field_info.annotation),
                    "description": field_info.description or "",
                })

        return {
            "sortable_fields": sortable,
            "filter_params": filter_params,
        }

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
    def _apply_pagination(cls, query: Select, params: BaseFilterParams) -> Select:
        """Apply pagination"""
        return query.offset(params.offset).limit(params.page_size)

    @classmethod
    def _apply_sorting(cls, query: Select, params: BaseFilterParams) -> Select:
        """Apply sorting using SortOrder enum"""
        if params.sort_by and params.sort_by in cls.SORTABLE_FIELDS:
            column = getattr(cls.MODEL, params.sort_by, None)
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
    def _apply_soft_delete(cls, query: Select, params: BaseFilterParams) -> Select:
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
