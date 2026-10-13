"""
CertificateFilterParams - Strongly typed filter parameters of GET /certificates/
"""

from typing import Literal, Optional

from pydantic import Field

from app.backend.api.v1.models.query.base import BaseFilterParams
from app.backend.enums.general import CertificateSource, CertificateState

# Fields the results can be sorted by; sort_by accepts exactly these (another value answers 422)
CertificateSortField = Literal["id", "label", "not_after", "created_at"]


class CertificateFilterParams(BaseFilterParams):
    """Filter parameters of the certificates across users"""

    sort_by: Optional[CertificateSortField] = Field(default=None, description="Field to sort by")

    state: Optional[CertificateState] = Field(
        default=None, description="active, expired (not_after in the past) or revoked; computed when read")
    source: Optional[CertificateSource] = Field(
        default=None, description="issued (by this server), uploaded (a file), legacy (from before the upgrade) or "
                                  "configured (the development seed)")
    user_id: Optional[int] = Field(default=None, ge=1, description="The certificates of this user")
    post_id: Optional[int] = Field(
        default=None, ge=1, description="The certificates of the users whose home post this is (certificate.view and "
                                        "user.view must be in effect there, 403 otherwise)")
    expires_within_days: Optional[int] = Field(
        default=None, ge=0, le=3650,
        description="Not yet expired and expiring within this many days (with state=active: revoked ones left out)")
