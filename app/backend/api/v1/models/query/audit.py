"""AuditFilterParams - the filters of the audit log list (services/audit/query.py AuditCriteria, the same names).
AuditEventCode (every code of the catalog) lives here; the response models of v1 import it."""
from typing import List, Literal, Optional

from pydantic import Field, ValidationInfo, field_validator

from app.backend.api.v1.models.query.base import BaseFilterParams
from app.backend.enums.audit import AuditCategory, AuditEvent
from app.backend.enums.errors import FieldErrorCode
from app.backend.enums.general import SortOrder
from app.backend.errors.coded import invalid
from app.backend.utils.validators import UtcDatetime

AuditEventCode = Literal[tuple(event.code for event in AuditEvent)]  # type: ignore[valid-type]
_NEWEST_FIRST = (SortOrder.DESCENDING, SortOrder.DESCENDING_NULLS_FIRST, SortOrder.DESCENDING_NULLS_LAST)

# The list is ordered by id (newest first by default); before_id pages through it
AuditSortField = Literal["id"]


class AuditFilterParams(BaseFilterParams):
    """Filters of GET /audit-logs/ (several given: all must hold)"""

    sort_by: Optional[AuditSortField] = Field(default=None, description="Field to sort by (id)")
    sort_order: SortOrder = Field(default=SortOrder.DESCENDING, description="Sort order; newest first by default")

    event: Optional[List[AuditEventCode]] = Field(
        default=None, description="Any of these events: ?event=user.login&event=session.logout. Matches the stored "
                                  "event; a row served as legacy.unknown carries it in params.stored_event. "
                                  "legacy.unknown also matches rows without an event or with a code the catalog "
                                  "no longer has")
    category: Optional[List[AuditCategory]] = Field(default=None, description="Any of these categories")
    user_id: Optional[int] = Field(default=None, description="Who acted")
    resource_type: Optional[str] = Field(default=None, max_length=50, description="Exact resource type")
    resource_id: Optional[int] = Field(default=None, description="Exact resource id")
    success: Optional[bool] = Field(default=None, description="Only successful (true) or refused (false) events")
    created_from: Optional[UtcDatetime] = Field(default=None, description="Written at or after")
    created_to: Optional[UtcDatetime] = Field(default=None, description="Written at or before")
    request_id: Optional[str] = Field(default=None, max_length=32, description="X-Request-ID of the request")
    session_id: Optional[str] = Field(default=None, max_length=128,
                                      description="Login session (needs audit.view: matches nothing without it)")
    post_id: Optional[int] = Field(default=None, ge=1, description="Events at this post")
    group_id: Optional[int] = Field(default=None, ge=1, description="Events at this scope group or anywhere below it (the "
                                                              "tree as it is now)")
    corps_id: Optional[int] = Field(default=None, ge=1, description="Events in this corps (its brigades and posts "
                                                              "included); an id of another kind gives nothing")
    brigade_id: Optional[int] = Field(default=None, ge=1, description="Events in this brigade (its posts included); an "
                                                                "id of another kind gives nothing")
    before_id: Optional[int] = Field(default=None, ge=1, description="Only entries with a smaller id: the id of the "
                                                                     "last entry of the previous page. It pages by "
                                                                     "itself: only with page 1 and a descending "
                                                                     "sort_order (otherwise 422 "
                                                                     "before_id_needs_first_page_newest_first)")

    @field_validator("before_id")
    @classmethod
    def keyset_alone(cls, value: Optional[int], info: ValidationInfo) -> Optional[int]:
        """A keyset next to an offset or an ascending order would skip or repeat rows unnoticed"""
        if value is not None and (info.data.get("page", 1) != 1 or info.data.get("sort_order") not in _NEWEST_FIRST):
            raise invalid(FieldErrorCode.BEFORE_ID_NEEDS_FIRST_PAGE_NEWEST_FIRST)
        return value
