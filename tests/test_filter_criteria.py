"""Every query model of v1 converts into the criteria of the service that filters with it.

A filter service declares what it filters by (a frozen dataclass of criteria); the query model of a version is
that version's contract. A field the version accepts but the service does not know would be dropped silently,
so the conversion refuses it (TypeError) and this test names it before any request does.
"""
import dataclasses
from typing import get_args

import pytest

from app.backend.api.v1.models.query.audit import AuditFilterParams, AuditSortField
from app.backend.api.v1.models.query.categories import CategoryFilterParams, CategorySortField
from app.backend.api.v1.models.query.certificates import CertificateFilterParams, CertificateSortField
from app.backend.api.v1.models.query.guides import GuideFilterParams, GuideSortField
from app.backend.api.v1.models.query.media import MediaFilterParams, MediaSortField
from app.backend.api.v1.models.query.media_formats import MediaFormatFilterParams, MediaFormatSortField
from app.backend.api.v1.models.query.observations import ObservationFilterParams, ObservationSortField
from app.backend.api.v1.models.query.posts import PostFilterParams, PostSortField
from app.backend.api.v1.models.query.roles import RoleFilterParams, RoleSortField
from app.backend.api.v1.models.query.signal_types import SignalTypeFilterParams, SignalTypeSortField
from app.backend.api.v1.models.query.tags import TagFilterParams, TagSortField
from app.backend.api.v1.models.query.users import UserFilterParams, UserSortField
from app.backend.api.v1.models.request.observations import ObservationReportParams
from app.backend.enums.general import ObservationReportType
from app.backend.filters.category_filter import CategoryFilterService
from app.backend.filters.certificate_filter import CertificateFilterService
from app.backend.filters.media_filter import MediaFilterService
from app.backend.filters.media_format_filter import MediaFormatFilterService
from app.backend.filters.observation_filter import ObservationFilterService
from app.backend.filters.observation_report_filter import ObservationReportCriteria
from app.backend.filters.post_filter import PostFilterService
from app.backend.filters.role_filter import RoleFilterService
from app.backend.filters.signal_type_filter import SignalTypeFilterService
from app.backend.filters.simple_engine import as_criteria
from app.backend.filters.tag_filter import TagFilterService
from app.backend.filters.user_filter import UserFilterService
from app.backend.services.audit.query import AuditFilterService
from app.backend.services.guides import GuideFilterService

V1_LISTS = [
    (AuditFilterParams, AuditSortField, AuditFilterService),
    (CategoryFilterParams, CategorySortField, CategoryFilterService),
    (CertificateFilterParams, CertificateSortField, CertificateFilterService),
    (GuideFilterParams, GuideSortField, GuideFilterService),
    (MediaFilterParams, MediaSortField, MediaFilterService),
    (MediaFormatFilterParams, MediaFormatSortField, MediaFormatFilterService),
    (ObservationFilterParams, ObservationSortField, ObservationFilterService),
    (PostFilterParams, PostSortField, PostFilterService),
    (RoleFilterParams, RoleSortField, RoleFilterService),
    (SignalTypeFilterParams, SignalTypeSortField, SignalTypeFilterService),
    (TagFilterParams, TagSortField, TagFilterService),
    (UserFilterParams, UserSortField, UserFilterService),
]
IDS = [query.__name__ for query, _, _ in V1_LISTS]


def _criteria_fields(criteria_class) -> set:
    return {field.name for field in dataclasses.fields(criteria_class)}


@pytest.mark.parametrize("query, sort_field, service", V1_LISTS, ids=IDS)
def test_query_model_converts_into_the_service_criteria(query, sort_field, service):
    assert set(query.model_fields) <= _criteria_fields(service.CRITERIA)
    criteria = service.criteria(query(page=2, page_size=10))
    assert isinstance(criteria, service.CRITERIA) and criteria.offset == 10


@pytest.mark.parametrize("query, sort_field, service", V1_LISTS, ids=IDS)
def test_every_sort_field_of_v1_is_sortable_by_the_service(query, sort_field, service):
    assert set(get_args(sort_field)) <= set(service.SORTABLE_FIELDS)


def test_report_body_converts_into_the_report_criteria():
    assert set(ObservationReportParams.model_fields) <= _criteria_fields(ObservationReportCriteria)
    criteria = as_criteria(ObservationReportCriteria, ObservationReportParams(report_type="all_fields", tag_ids=[7]))
    assert criteria.report_type is ObservationReportType.ALL_FIELDS and criteria.tag_ids == [7]


def test_a_field_the_service_does_not_know_is_refused():
    class WithUnknownField(TagFilterParams):
        colour: str = "red"

    with pytest.raises(TypeError):
        TagFilterService.criteria(WithUnknownField())
