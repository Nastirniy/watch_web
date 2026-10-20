"""
UnitFilterService - Filter service for units (corps, brigades: scope groups of one kind)
"""

from dataclasses import dataclass
from typing import FrozenSet, Optional
from sqlalchemy.sql.selectable import Select

from app.backend.enums.general import ScopeGroupKind
from app.backend.models.scope_group import ScopeGroup
from app.backend.filters.simple_engine import ListCriteria, SimpleFilterEngine


@dataclass(frozen=True, kw_only=True)
class UnitCriteria(ListCriteria):
    """What UnitFilterService filters by; each API version converts its query model into it"""
    id: Optional[int] = None
    name_contains: Optional[str] = None
    corps_id: Optional[int] = None      # the parent of a brigade
    include_deleted: bool = False


class UnitFilterService(SimpleFilterEngine):
    """Filter service for units of one kind"""

    MODEL = ScopeGroup
    CRITERIA = UnitCriteria
    SORTABLE_FIELDS = ["id", "name", "created_at", "updated_at"]

    @classmethod
    def apply_filters(
        cls,
        query: Select,
        params: UnitCriteria,
        kind: ScopeGroupKind,
        visible_ids: Optional[FrozenSet[int]] = None,
        archived_ids: Optional[FrozenSet[int]] = None,
    ) -> Select:
        """Apply all filters from UnitCriteria to the groups of this kind. visible_ids: the groups the caller sees
        (None: every group); archived_ids: with include_deleted, the groups shown even when archived (None: every
        one)"""
        params = cls.criteria(params)
        query = cls.apply_filters_for_count(query, params, kind, visible_ids, archived_ids)
        query = cls._apply_sorting(query, params)
        return cls._apply_pagination(query, params)

    @classmethod
    def apply_filters_for_count(
        cls,
        query: Select,
        params: UnitCriteria,
        kind: ScopeGroupKind,
        visible_ids: Optional[FrozenSet[int]] = None,
        archived_ids: Optional[FrozenSet[int]] = None,
    ) -> Select:
        """Apply filters without sorting and pagination"""
        params = cls.criteria(params)
        query = query.where(ScopeGroup.kind == ScopeGroupKind(kind).value)
        if visible_ids is not None:
            query = query.where(ScopeGroup.id.in_(sorted(visible_ids)))
        # Archived units only on include_deleted, and only where scope_group.archive is in effect
        query = cls._apply_soft_delete(query, params, ScopeGroup.id, archived_ids)
        query = cls._apply_exact(query, ScopeGroup.id, params.id)
        query = cls._apply_exact(query, ScopeGroup.parent_id, params.corps_id)
        return cls._apply_contains(query, ScopeGroup.name, params.name_contains)
