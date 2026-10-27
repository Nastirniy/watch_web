"""AuditFilterParams - the filters of the audit log list (services/audit/query.py AuditCriteria, the same names)"""
from typing import List, Literal, Optional

from pydantic import Field

from app.backend.api.v1.models.query.base import BaseFilterParams
from app.backend.api.v1.models.response.audit import AuditEventCode
from app.backend.enums.audit import AuditCategory
from app.backend.enums.general import SortOrder
from app.backend.utils.validators import UtcDatetime

# The list is ordered by id (newest first by default); before_id pages through it
AuditSortField = Literal["id"]


class AuditFilterParams(BaseFilterParams):
    """Filters of GET /audit-logs/ (several given: all must hold)"""

    sort_by: Optional[AuditSortField] = Field(default=None, description="Field to sort by (id)")
    sort_order: SortOrder = Field(default=SortOrder.DESCENDING, description="Sort order; newest first by default")

    event: Optional[List[AuditEventCode]] = Field(
        default=None, description="Any of these events: ?event=user.login&event=session.logout; legacy.unknown also "
                                  "matches rows written before the catalog")
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
    post_id: Optional[int] = Field(default=None, description="Events at this post")
    group_id: Optional[int] = Field(default=None, description="Events at this scope group or anywhere below it (the "
                                                              "tree as it is now)")
    corps_id: Optional[int] = Field(default=None, description="Events in this corps (its brigades and posts "
                                                              "included); an id of another kind gives nothing")
    brigade_id: Optional[int] = Field(default=None, description="Events in this brigade (its posts included); an "
                                                                "id of another kind gives nothing")
    before_id: Optional[int] = Field(default=None, ge=1, description="Only entries with a smaller id: the id of the "
                                                                     "last entry of the previous page")
