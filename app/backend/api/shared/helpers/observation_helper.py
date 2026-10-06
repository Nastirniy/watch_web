from typing import List

from sqlalchemy.orm import selectinload
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.models.observation import Observation, ObservationFrequency
from app.backend.services.access_control import AccessControlService
from app.backend.services.auth_context import AuthContext
from app.backend.services.api_errors import ApiError
from app.backend.enums.errors import ErrorCode


async def get_observation_for_write(
        session: AsyncSession,
        auth: AuthContext,
        observation_id: int,
        lock: bool = False,
) -> Observation:
    """Observation the caller may modify: 404 when missing or archived, 403 without post write access or ownership.
    lock=True takes a row lock (MySQL) so concurrent band changes of one observation run one after another."""
    query = select(Observation).where(Observation.id == observation_id).options(selectinload(Observation.all_frequencies))
    if lock:
        query = query.with_for_update()
    observation = (await session.exec(query)).first()
    if not observation:
        raise ApiError(ErrorCode.OBSERVATION_NOT_FOUND)

    AccessControlService.check_post_write(auth, observation.post_id, "observation")
    AccessControlService.check_observation_ownership(auth, observation)

    if observation.is_deleted:
        raise ApiError(ErrorCode.OBSERVATION_NOT_FOUND)
    return observation


def get_frequency_or_404(observation: Observation, frequency_id: int) -> ObservationFrequency:
    """Band of this observation (archived ones included; callers decide what an archived band means)"""
    for frequency in observation.all_frequencies:
        if frequency.id == frequency_id:
            return frequency
    raise ApiError(ErrorCode.FREQUENCY_NOT_FOUND)


def ensure_other_active_frequency(observation: Observation, frequency_id: int) -> None:
    """An observation keeps at least one active band of any kind: 409 when frequency_id is the last one.
    Load the observation with get_observation_for_write(lock=True) so the band list cannot change meanwhile."""
    if not any(f.id != frequency_id for f in observation.frequencies):
        raise ApiError(ErrorCode.LAST_FREQUENCY_NOT_ARCHIVABLE)


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
