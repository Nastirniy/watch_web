from typing import List, Optional
from datetime import datetime, timezone
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from pydantic import BaseModel

from app.backend.database import get_session
from app.backend.models.observation import Observation
from app.backend.models.user import User
from app.backend.dependencies import get_current_user
from app.backend.services.permissions import PermissionService
from app.backend.enums.general import Permissions, ObservationStatus
from app.backend.enums.routes import ObservationRoutes, SystemRoutes

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


@router.get(SystemRoutes.ROOT.value, response_model=List[Observation])
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


@router.post(SystemRoutes.ROOT.value, response_model=Observation)
@PermissionService.require_permission(Permissions.OBSERVATION_CREATE.value)
async def create_observation(
        observation_data: ObservationCreate,
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
    """Create new observation"""
    observation = Observation(
        **observation_data.model_dump(),
        user_id=current_user.id,
        frequency_center=(observation_data.frequency_min + observation_data.frequency_max) / 2,
        bandwidth=observation_data.frequency_max - observation_data.frequency_min
    )

    session.add(observation)

    # Update user's last record created
    current_user.last_record_created_at = datetime.now(timezone.utc)
    session.add(current_user)

    try:
        await session.commit()
        await session.refresh(observation)
        return observation
    except IntegrityError as e:
        await session.rollback()
        # Check if it's a foreign key constraint error
        if "post_id" in str(e) or "FOREIGN KEY" in str(e):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid post_id: Post does not exist"
            )
        elif "signal_type_id" in str(e):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid signal_type_id: Signal type does not exist"
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Database constraint violation"
            )


@router.post(ObservationRoutes.MARK_DISAPPEARED.value)
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
    observation.disappeared_at = datetime.now(timezone.utc)
    observation.disappeared_by = current_user.id
    observation.updated_at = datetime.now(timezone.utc)

    session.add(observation)
    await session.commit()

    return {"message": "Observation marked as disappeared"}