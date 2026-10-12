"""
ObservationFrequencyFilter - the only way to read observation_frequencies.

A band is visible when the band and its observation are both not archived and
the observation's post is inside the caller's scope. Archive state and post
live on observations, so every band query here joins observations; reading
the table directly would leak archived observations and other posts into
lists and reports.

Indexes used (see ObservationFrequency.__table_args__):
- (observation_id, is_deleted): bands of given observations, "has an active band" checks
- (kind, frequency_center): range filters for one kind
- (frequency_center): range filters over every kind
The join to observations goes through its primary key.
"""

from decimal import Decimal
from typing import Iterable, List, Optional, Union

from sqlalchemy import and_, func, or_
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.selectable import Select
from sqlmodel import select

from app.backend.enums.general import FrequencyKind
from app.backend.models.observation import Observation, ObservationFrequency


class ObservationFrequencyFilter:
    """Query builders for frequency bands"""

    @staticmethod
    def band_conditions(
        kind: Optional[Union[FrequencyKind, str]] = None,
        center_from: Optional[Decimal] = None,
        center_to: Optional[Decimal] = None,
        bandwidth_from: Optional[Decimal] = None,
        bandwidth_to: Optional[Decimal] = None,
    ) -> List[ColumnElement]:
        """Conditions on one band; combine them in one query so they match the same band"""
        conditions = []
        if kind is not None:
            conditions.append(ObservationFrequency.kind == (kind.value if isinstance(kind, FrequencyKind) else kind))
        if center_from is not None:
            conditions.append(ObservationFrequency.frequency_center >= center_from)
        if center_to is not None:
            conditions.append(ObservationFrequency.frequency_center <= center_to)
        if bandwidth_from is not None:
            conditions.append(ObservationFrequency.bandwidth >= bandwidth_from)
        if bandwidth_to is not None:
            conditions.append(ObservationFrequency.bandwidth <= bandwidth_to)
        return conditions

    @staticmethod
    def observation_has_band(conditions: List[ColumnElement]) -> ColumnElement:
        """Condition on Observation: at least one active band matches every condition"""
        return Observation.id.in_(
            select(ObservationFrequency.observation_id).where(
                ObservationFrequency.is_deleted == False,
                *conditions,
            )
        )

    @staticmethod
    def lowest_center(conditions: List[ColumnElement]) -> ColumnElement:
        """Correlated scalar subquery: lowest frequency_center among the observation's active bands
        matching every condition. Used as an ORDER BY target for observations.

        Expensive on large tables: it runs once per candidate observation and the sort cannot
        use an index, so the database evaluates it for every row that passes the filters before
        LIMIT applies. Keep it behind filters (post, dates) and pagination."""
        return (
            select(func.min(ObservationFrequency.frequency_center))
            .where(
                ObservationFrequency.observation_id == Observation.id,
                ObservationFrequency.is_deleted == False,
                *conditions,
            )
            .correlate(Observation)
            .scalar_subquery()
        )

    @staticmethod
    def rows_query(
        access_post_ids: Optional[Iterable[int]] = None,
        include_deleted: bool = False,
        archived_post_ids: Optional[Iterable[int]] = None,
    ) -> Select:
        """Bands joined with their observation, archive flags and post scope applied.
        include_deleted also returns archived bands and bands of archived observations, at archived_post_ids only
        (None: every post)."""
        query = select(ObservationFrequency).join(
            Observation, Observation.id == ObservationFrequency.observation_id
        )
        live = and_(ObservationFrequency.is_deleted == False, Observation.is_deleted == False)
        if not include_deleted:
            query = query.where(live)
        elif archived_post_ids is not None:
            archived = sorted(archived_post_ids)
            query = query.where(or_(live, Observation.post_id.in_(archived)) if archived else live)
        if access_post_ids is not None:
            query = query.where(Observation.post_id.in_(list(access_post_ids)))
        return query

    @staticmethod
    def of_observation(observation_id: int, include_deleted: bool = False) -> Select:
        """Bands of one observation whose access was already checked by the caller"""
        query = select(ObservationFrequency).where(ObservationFrequency.observation_id == observation_id)
        if not include_deleted:
            query = query.where(ObservationFrequency.is_deleted == False)
        return query.order_by(ObservationFrequency.id)
