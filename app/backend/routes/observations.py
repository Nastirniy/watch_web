from typing import List, Optional
from datetime import datetime
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select
from pydantic import BaseModel

from app.backend.database import get_session
from app.backend.models.observation import Observation
from app.backend.models.user import User
from app.backend.dependencies import get_current_user
from app.backend.services.permissions import PermissionService
from app.backend.enums import Routes, Permissions, ObservationStatus

router = APIRouter()


class ObservationCreate(BaseModel):
    post_id: int
    signal_type_id: int
    frequency_min: Decimal
    frequency_max: Decimal
    observed_at: datetime
    duration_seconds: Optional[int] = None
    signal_strength: Optional[Decimal] = None
    modulation_type_id: Optional[int] = None
    comment: Optional[str] = None
    confidence_level: Optional[str] = None


@router.get(Routes.OBSERVATIONS_LIST.value.replace("/observations", ""), response_model=List[Observation])
@PermissionService.require_permission(Permissions.OBSERVATION_VIEW.value)
async def list_observations(
        skip: int = 0,
        limit: int = 100,
        post_id: Optional[int] = None,
        status: Optional[str] = None,
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
    """List observations"""
    query = select(Observation).where(Observation.is_deleted == False)

    if post_id:
        query = query.where(Observation.post_id == post_id)
    if status:
        query = query.where(Observation.status == status)

    result = await session.exec(query.offset(skip).limit(limit))
    return result.all()


@router.post(Routes.OBSERVATIONS_CREATE.value.replace("/observations", ""), response_model=Observation)
@PermissionService.require_permission(Permissions.OBSERVATION_CREATE.value)
async def create_observation(
        observation_data: ObservationCreate,
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
    """Create new observation"""
    observation = Observation(
        **observation_data.dict(),
        user_id=current_user.id,
        frequency_center=(observation_data.frequency_min + observation_data.frequency_max) / 2,
        bandwidth=observation_data.frequency_max - observation_data.frequency_min
    )

    session.add(observation)

    # Update user's last record created
    current_user.last_record_created_at = datetime.utcnow()
    session.add(current_user)

    await session.commit()
    await session.refresh(observation)

    return observation


@router.post(Routes.OBSERVATIONS_MARK_DISAPPEARED.value.replace("/observations/", "").replace("/disappeared",
                                                                                              "") + "/disappeared")
@PermissionService.require_permission(Permissions.OBSERVATION_UPDATE.value)
async def mark_observation_disappeared(
        observation_id: int,
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
    """Mark observation as disappeared"""
    result = await session.exec(
        select(Observation).where(
            Observation.id == observation_id,
            Observation.is_deleted == False
        )
    )
    observation = result.first()

    if not observation:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Observation not found"
        )

    observation.status = ObservationStatus.DISAPPEARED.value
    observation.disappeared_at = datetime.utcnow()
    observation.disappeared_by = current_user.id
    observation.updated_at = datetime.utcnow()

    session.add(observation)
    await session.commit()

    return {"message": "Observation marked as disappeared"}