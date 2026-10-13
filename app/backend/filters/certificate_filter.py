"""
CertificateFilterService - Filter service for the client certificates of users (GET /certificates/)
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from sqlalchemy.sql.selectable import Select

from app.backend.enums.general import CertificateSource, CertificateState
from app.backend.filters.simple_engine import ListCriteria, SimpleFilterEngine
from app.backend.models.user import User
from app.backend.models.user_certificate import UserCertificate
from app.backend.services.certificates import state_clause


@dataclass(frozen=True, kw_only=True)
class CertificateCriteria(ListCriteria):
    """What CertificateFilterService filters by; each API version converts its query model into it"""
    state: Optional[CertificateState] = None
    source: Optional[CertificateSource] = None
    user_id: Optional[int] = None
    post_id: Optional[int] = None            # the home post of the certificate's user
    expires_within_days: Optional[int] = None


class CertificateFilterService(SimpleFilterEngine):
    """Certificates of non-archived users, in the scope of the caller (the home posts of their users)"""

    MODEL = UserCertificate
    CRITERIA = CertificateCriteria
    SORTABLE_FIELDS = ["id", "label", "not_after", "created_at"]

    @classmethod
    def apply_filters(
        cls,
        query: Select,
        params: CertificateCriteria,
        home_post_ids: Optional[Iterable[int]] = None,
    ) -> Select:
        """query selects UserCertificate. home_post_ids: the posts whose users' certificates may be listed (None:
        every user, users without a home post included; otherwise only users whose home post is in it)."""
        params = cls.criteria(params)

        query = query.join(User, User.id == UserCertificate.user_id).where(User.is_deleted == False)
        if home_post_ids is not None:
            query = query.where(User.post_id.in_(sorted(home_post_ids)))

        if params.state is not None:
            query = query.where(state_clause(CertificateState(params.state)))
        if params.source is not None:
            query = query.where(UserCertificate.source == CertificateSource(params.source).value)
        query = cls._apply_exact(query, UserCertificate.user_id, params.user_id)
        query = cls._apply_exact(query, User.post_id, params.post_id)
        if params.expires_within_days is not None:
            # Not yet expired and expiring within the days (combine with state=active to leave out revoked ones)
            now = datetime.now(timezone.utc)
            query = cls._apply_range(query, UserCertificate.not_after, now,
                                     now + timedelta(days=params.expires_within_days))

        query = cls._apply_sorting(query, params)
        query = cls._apply_pagination(query, params)
        return query
