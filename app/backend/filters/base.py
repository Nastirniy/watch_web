"""
Base filter engine for flexible entity filtering.
Provides reusable components for building type-safe, permission-aware queries.
"""

from enum import Enum
from typing import Any, List, Optional, Type
from pydantic import BaseModel, field_validator
from sqlalchemy import and_, or_
from sqlalchemy.sql.selectable import Select
from sqlmodel import select
from loguru import logger

from app.backend.services.api_errors import invalid
from app.backend.enums.errors import FieldErrorCode


class FilterOperator(str, Enum):
    """Supported filter operators for query conditions"""
    EQ = "eq"              # equals
    NEQ = "neq"            # not equals
    GT = "gt"              # greater than
    GTE = "gte"            # greater or equal
    LT = "lt"              # less than
    LTE = "lte"            # less or equal
    CONTAINS = "contains"  # string contains (case-insensitive)
    STARTS = "starts"      # starts with
    ENDS = "ends"          # ends with
    IN = "in"              # in list
    NOT_IN = "not_in"      # not in list
    BETWEEN = "between"    # between two values [min, max]
    IS_NULL = "is_null"    # is null
    IS_NOT_NULL = "is_not_null"  # is not null


class FilterCondition(BaseModel):
    """Single filter condition"""
    field: str
    operator: FilterOperator = FilterOperator.EQ
    value: Any = None

    model_config = {"extra": "forbid"}


class SortOrder(BaseModel):
    """Sort specification"""
    field: str
    desc: bool = False

    model_config = {"extra": "forbid"}


class BaseFilterParams(BaseModel):
    """Base filter parameters all entities share"""
    conditions: List[FilterCondition] = []
    logic: str = "and"  # "and" or "or"
    sort: List[SortOrder] = []
    page: int = 1
    page_size: int = 50
    include_deleted: bool = False

    model_config = {"extra": "forbid"}

    @field_validator('page')
    @classmethod
    def validate_page(cls, v: int) -> int:
        return max(1, v)

    @field_validator('page_size')
    @classmethod
    def validate_page_size(cls, v: int) -> int:
        return min(max(1, v), 200)  # Max 200 per page

    @field_validator('logic')
    @classmethod
    def validate_logic(cls, v: str) -> str:
        if v not in ("and", "or"):
            raise invalid(FieldErrorCode.SEARCH_LOGIC_INVALID)
        return v


class BaseFilterEngine:
    """
    Base class for entity-specific filter services.

    Subclasses must define:
    - MODEL: The SQLModel class to filter
    - ALLOWED_FIELDS: List of field names that can be filtered
    - SORTABLE_FIELDS: List of field names that can be sorted
    """

    MODEL: Type = None
    ALLOWED_FIELDS: List[str] = []
    SORTABLE_FIELDS: List[str] = []

    @classmethod
    def _apply_condition(cls, condition: FilterCondition):
        """
        Build a WHERE clause for a single filter condition.
        Returns the clause or None if invalid.
        """
        if condition.field not in cls.ALLOWED_FIELDS:
            logger.warning(f"Filter field '{condition.field}' not in allowed fields for {cls.__name__}")
            return None

        column = getattr(cls.MODEL, condition.field, None)
        if column is None:
            logger.warning(f"Column '{condition.field}' not found on model {cls.MODEL.__name__}")
            return None

        op = condition.operator
        val = condition.value

        try:
            if op == FilterOperator.EQ:
                return column == val
            elif op == FilterOperator.NEQ:
                return column != val
            elif op == FilterOperator.GT:
                return column > val
            elif op == FilterOperator.GTE:
                return column >= val
            elif op == FilterOperator.LT:
                return column < val
            elif op == FilterOperator.LTE:
                return column <= val
            elif op == FilterOperator.CONTAINS:
                return column.ilike(f"%{val}%")
            elif op == FilterOperator.STARTS:
                return column.ilike(f"{val}%")
            elif op == FilterOperator.ENDS:
                return column.ilike(f"%{val}")
            elif op == FilterOperator.IN:
                if not isinstance(val, (list, tuple)):
                    logger.warning(f"IN operator requires list value, got {type(val)}")
                    return None
                return column.in_(val)
            elif op == FilterOperator.NOT_IN:
                if not isinstance(val, (list, tuple)):
                    logger.warning(f"NOT_IN operator requires list value, got {type(val)}")
                    return None
                return column.not_in(val)
            elif op == FilterOperator.BETWEEN:
                if not isinstance(val, (list, tuple)) or len(val) != 2:
                    logger.warning(f"BETWEEN operator requires [min, max] list, got {val}")
                    return None
                return column.between(val[0], val[1])
            elif op == FilterOperator.IS_NULL:
                return column.is_(None)
            elif op == FilterOperator.IS_NOT_NULL:
                return column.isnot(None)
        except Exception as e:
            logger.error(f"Error building condition for {condition.field}: {e}")
            return None

        return None

    @classmethod
    def apply_filters(
        cls,
        query: Select,
        params: BaseFilterParams,
        access_filter_post_id: Optional[int] = None
    ) -> Select:
        """
        Apply all filter conditions to query.

        Args:
            query: Base SQLModel select query
            params: Filter parameters
            access_filter_post_id: Optional post_id for row-level access control

        Returns:
            Modified query with all filters applied
        """
        # Apply soft-delete filter if model has is_deleted field
        include_deleted = getattr(params, 'include_deleted', False)
        if hasattr(cls.MODEL, 'is_deleted') and not include_deleted:
            query = query.where(cls.MODEL.is_deleted == False)

        # Apply access control filter if provided and model has post_id
        if access_filter_post_id is not None and hasattr(cls.MODEL, 'post_id'):
            query = query.where(cls.MODEL.post_id == access_filter_post_id)

        # Build and apply conditions with AND/OR logic
        if params.conditions:
            condition_clauses = []
            for cond in params.conditions:
                clause = cls._apply_condition(cond)
                if clause is not None:
                    condition_clauses.append(clause)

            if condition_clauses:
                if params.logic == "or":
                    query = query.where(or_(*condition_clauses))
                else:
                    query = query.where(and_(*condition_clauses))

        # Apply sorting
        for sort_order in params.sort:
            if sort_order.field in cls.SORTABLE_FIELDS:
                column = getattr(cls.MODEL, sort_order.field)
                query = query.order_by(column.desc() if sort_order.desc else column)
            else:
                logger.warning(f"Sort field '{sort_order.field}' not in sortable fields for {cls.__name__}")

        # Apply pagination
        offset = (params.page - 1) * params.page_size
        query = query.offset(offset).limit(params.page_size)

        return query

    @classmethod
    def apply_filters_for_count(
        cls,
        query: Select,
        params: BaseFilterParams,
        access_filter_post_id: Optional[int] = None
    ) -> Select:
        """
        Apply filters without pagination (for counting total results).

        Args:
            query: Base SQLModel select query (typically select(func.count()))
            params: Filter parameters
            access_filter_post_id: Optional post_id for row-level access control

        Returns:
            Modified query with filters but no pagination
        """
        # Apply soft-delete filter
        include_deleted = getattr(params, 'include_deleted', False)
        if hasattr(cls.MODEL, 'is_deleted') and not include_deleted:
            query = query.where(cls.MODEL.is_deleted == False)

        # Apply access control filter
        if access_filter_post_id is not None and hasattr(cls.MODEL, 'post_id'):
            query = query.where(cls.MODEL.post_id == access_filter_post_id)

        # Build and apply conditions
        if params.conditions:
            condition_clauses = []
            for cond in params.conditions:
                clause = cls._apply_condition(cond)
                if clause is not None:
                    condition_clauses.append(clause)

            if condition_clauses:
                if params.logic == "or":
                    query = query.where(or_(*condition_clauses))
                else:
                    query = query.where(and_(*condition_clauses))

        return query

    @classmethod
    def get_base_query(cls) -> Select:
        """Get base query for this entity"""
        return select(cls.MODEL)
