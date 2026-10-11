from datetime import datetime
from typing import List

from sqlalchemy.orm import selectinload
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.models.observation import Observation, ObservationFrequency
from app.backend.auth.access_control import AccessControlService
from app.backend.auth.context import AuthContext
from app.backend.api.shared.helpers.post_helper import ensure_post_not_archived
from app.backend.errors.problems import ApiError, field_problem
from app.backend.enums.errors import ErrorCode, FieldErrorCode
from app.backend.filters.observation_frequency_filter import ObservationFrequencyFilter
from app.backend.utils.datetime_utils import as_utc
from app.backend.utils.validators import INT_MAX


async def get_observation_for_write(
        session: AsyncSession,
        auth: AuthContext,
        observation_id: int,
        lock: bool = False,
) -> Observation:
    """Observation the caller may modify: 404 when missing or archived, 403 without post write access or ownership,
    409 post_archived when its post is archived (after the 403 checks).
    lock=True takes a row lock (MySQL) so concurrent band changes of one observation run one after another."""
    query = select(Observation).where(Observation.id == observation_id).options(selectinload(Observation.all_frequencies))
    if lock:
        query = query.with_for_update()
    observation = (await session.exec(query)).first()
    if not observation:
        raise ApiError(ErrorCode.OBSERVATION_NOT_FOUND)

    AccessControlService.check_post_write(auth, observation.post_id, "observation")
    AccessControlService.check_observation_ownership(auth, observation)
    await ensure_post_not_archived(session, observation.post_id)

    if observation.is_deleted:
        raise ApiError(ErrorCode.OBSERVATION_NOT_FOUND)
    return observation


def get_frequency_or_404(observation: Observation, frequency_id: int) -> ObservationFrequency:
    """Band of this observation (archived ones included; callers decide what an archived band means)"""
    for frequency in observation.all_frequencies:
        if frequency.id == frequency_id:
            return frequency
    raise ApiError(ErrorCode.FREQUENCY_NOT_FOUND)


async def ensure_other_active_frequency(session: AsyncSession, observation_id: int, frequency_id: int) -> None:
    """An observation keeps at least one active band of any kind: 409 when frequency_id is the last one.
    Call it after get_observation_for_write(lock=True). The band list loaded with the observation is not covered by
    its row lock (a plain read of a snapshot on MySQL), so the other bands are read again with FOR UPDATE."""
    query = ObservationFrequencyFilter.of_observation(observation_id).where(ObservationFrequency.id != frequency_id)
    if (await session.exec(query.with_for_update().limit(1))).first() is None:
        raise ApiError(ErrorCode.LAST_FREQUENCY_NOT_ARCHIVABLE)


def disappeared_duration(observed_at: datetime, disappeared_at: datetime, field: str) -> int:
    """Seconds between the two instants; a field problem on `field` when disappeared_at is before observed_at
    or the duration does not fit the INT column"""
    delta = as_utc(disappeared_at) - as_utc(observed_at)
    duration = int(delta.total_seconds())
    if duration < 0:
        code = FieldErrorCode.DISAPPEARED_BEFORE_OBSERVED if field == "disappeared_at" else FieldErrorCode.OBSERVED_AFTER_DISAPPEARED
        raise field_problem(code, ("body", field))
    if duration > INT_MAX:
        raise field_problem(FieldErrorCode.VALUE_TOO_LARGE, ("body", field), max_value=INT_MAX)
    return duration


def frequency_audit_values(frequency: ObservationFrequency) -> dict:
    return frequency.model_dump(exclude={"bandwidth"})


def observation_audit_values(observation: Observation) -> dict:
    """Observation row plus its active bands, for audit old_values / new_values"""
    values = observation.model_dump()
    values["frequencies"] = [frequency_audit_values(f) for f in observation.frequencies]
    return values


def frequency_rows(items: List) -> List[ObservationFrequency]:
    """ORM rows from validated ObservationFrequencyCreate items; attach them through Observation.all_frequencies"""
    return [
        ObservationFrequency(
            kind=item.kind.value,
            link_group=item.link_group,
            frequency_min=item.frequency_min,
            frequency_max=item.frequency_max,
            frequency_center=item.frequency_center,
        )
        for item in items
    ]
