"""
ObservationTagFilter - observations by their tags: one condition for lists, searches and reports.

Archived tags count: they stay attached to their observations. observation_tags has the primary key
(observation_id, tag_id), so the number of matching rows of an observation is the number of the wanted
tags it has.
"""

from typing import Iterable

from sqlalchemy import func
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import select

from app.backend.enums.general import TagMatch
from app.backend.models.observation import Observation, ObservationTag


class ObservationTagFilter:
    """Query conditions on the tags of observations"""

    @staticmethod
    def observation_has_tags(tag_ids: Iterable[int], match: TagMatch = TagMatch.ANY) -> ColumnElement:
        """Condition on Observation: at least one of the tags (any) or every one of them (all)"""
        wanted = set(tag_ids)
        tagged = select(ObservationTag.observation_id).where(ObservationTag.tag_id.in_(wanted))
        if match == TagMatch.ALL:
            tagged = tagged.group_by(ObservationTag.observation_id).having(func.count(ObservationTag.tag_id) == len(wanted))
        return Observation.id.in_(tagged)
