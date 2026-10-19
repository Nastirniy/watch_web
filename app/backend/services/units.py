"""Units of the organisation (corps, brigades: scope groups of a unit kind): what a post or an observation belongs to.

The units of a post come from the scope tree every request loads (ScopeTree.units_of_post); the units of an
observation are the observation_units rows written at its create.
"""
from typing import Iterable

from sqlalchemy import delete
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.models.observation_unit import ObservationUnit


async def delete_observation_units(session: AsyncSession, observation_ids: Iterable[int]) -> int:
    """Remove the unit rows of these observations (a physical removal of observations, in its transaction; no route
    removes observations today). Returns the number of rows removed; the caller commits."""
    ids = sorted(set(observation_ids))
    if not ids:
        return 0
    result = await session.exec(delete(ObservationUnit).where(ObservationUnit.observation_id.in_(ids)))
    return result.rowcount
