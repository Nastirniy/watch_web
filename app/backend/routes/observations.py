from typing import List, Optional
from datetime import datetime, timezone
from decimal import Decimal
from fastapi import APIRouter, Depends, HTTPException, status as fapi_status
from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from pydantic import BaseModel
from sqlmodel.ext.asyncio.session import AsyncSession

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
    video_frequency_min: Optional[Decimal] = None
    video_frequency_max: Optional[Decimal] = None
    observed_at: Optional[datetime] = None
    duration_seconds: Optional[int] = None
    comment: Optional[str] = None
    confidence_level: Optional[str] = None


class ObservationUpdate(BaseModel):
    signal_type_id: Optional[int] = None
    frequency_min: Optional[Decimal] = None
    frequency_max: Optional[Decimal] = None
    video_frequency_min: Optional[Decimal] = None
    video_frequency_max: Optional[Decimal] = None
    observed_at: Optional[datetime] = None
    duration_seconds: Optional[int] = None
    comment: Optional[str] = None
    confidence_level: Optional[str] = None


class ObservationDisappeared(BaseModel):
    disappeared_at: datetime


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
    # Handle timezone for observed_at field
    observed_at = observation_data.observed_at
    if observed_at and observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    
    # Prepare observation data
    observation_dict = observation_data.model_dump()
    observation_dict["observed_at"] = observed_at
    observation_dict["user_id"] = current_user.id
    observation_dict["frequency_center"] = (observation_data.frequency_min + observation_data.frequency_max) / 2
    observation_dict["bandwidth"] = observation_data.frequency_max - observation_data.frequency_min
    
    # Calculate video frequency fields if both min and max are provided
    if observation_data.video_frequency_min is not None and observation_data.video_frequency_max is not None:
        observation_dict["video_frequency_center"] = (observation_data.video_frequency_min + observation_data.video_frequency_max) / 2
        observation_dict["video_bandwidth"] = observation_data.video_frequency_max - observation_data.video_frequency_min
    
    observation = Observation(**observation_dict)

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
                status_code=fapi_status.HTTP_400_BAD_REQUEST,
                detail="Invalid post_id: Post does not exist"
            )
        elif "signal_type_id" in str(e):
            raise HTTPException(
                status_code=fapi_status.HTTP_400_BAD_REQUEST,
                detail="Invalid signal_type_id: Signal type does not exist"
            )
        else:
            raise HTTPException(
                status_code=fapi_status.HTTP_400_BAD_REQUEST,
                detail="Database constraint violation"
            )


@router.put("/{observation_id}", response_model=Observation)
@PermissionService.require_permission(Permissions.OBSERVATION_UPDATE.value)
async def update_observation(
        observation_id: int,
        observation_data: ObservationUpdate,
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
    """Update existing observation"""
    # Get observation by ID
    result = await session.exec(
        select(Observation).where(
            Observation.id == observation_id,
            Observation.is_deleted == False
        )
    )
    observation = result.first()

    if not observation:
        raise HTTPException(
            status_code=fapi_status.HTTP_404_NOT_FOUND,
            detail="Observation not found"
        )

    # Update only provided fields
    update_data = observation_data.model_dump(exclude_unset=True)

    for field, value in update_data.items():
        # Handle timezone for observed_at field
        if field == "observed_at" and value and value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        setattr(observation, field, value)

    # Recalculate center frequency and bandwidth if min/max changed
    if observation_data.frequency_min is not None or observation_data.frequency_max is not None:
        observation.frequency_center = (observation.frequency_min + observation.frequency_max) / 2
        observation.bandwidth = observation.frequency_max - observation.frequency_min

    # Recalculate video frequency fields if both provided
    if observation_data.video_frequency_min is not None or observation_data.video_frequency_max is not None:
        if observation.video_frequency_min is not None and observation.video_frequency_max is not None:
            observation.video_frequency_center = (observation.video_frequency_min + observation.video_frequency_max) / 2
            observation.video_bandwidth = observation.video_frequency_max - observation.video_frequency_min

    observation.updated_at = datetime.now(timezone.utc)

    session.add(observation)

    try:
        await session.commit()
        await session.refresh(observation)
        return observation
    except IntegrityError as e:
        await session.rollback()
        if "signal_type_id" in str(e):
            raise HTTPException(
                status_code=fapi_status.HTTP_400_BAD_REQUEST,
                detail="Invalid signal_type_id: Signal type does not exist"
            )
        else:
            raise HTTPException(
                status_code=fapi_status.HTTP_400_BAD_REQUEST,
                detail="Database constraint violation"
            )


@router.post(ObservationRoutes.MARK_DISAPPEARED.value)
@PermissionService.require_permission(Permissions.OBSERVATION_UPDATE.value)
async def mark_observation_disappeared(
        observation_id: int,
        disappeared_data: ObservationDisappeared,
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
            status_code=fapi_status.HTTP_404_NOT_FOUND,
            detail="Observation not found"
        )

    # Handle timezone for disappeared_at field
    disappeared_at = disappeared_data.disappeared_at

    # Ensure disappeared_at has timezone info
    if disappeared_at.tzinfo is None:
        disappeared_at = disappeared_at.replace(tzinfo=timezone.utc)

    observation.status = ObservationStatus.DISAPPEARED.value
    observation.disappeared_at = disappeared_at
    observation.disappeared_by = current_user.id
    observation.updated_at = datetime.now(timezone.utc)

    # Calculate duration in seconds if observed_at exists
    if observation.observed_at:
        # Ensure both datetimes have timezone info for comparison
        observed_at = observation.observed_at
        if observed_at.tzinfo is None:
            observed_at = observed_at.replace(tzinfo=timezone.utc)

        time_delta = observation.disappeared_at - observed_at
        observation.duration_seconds = int(time_delta.total_seconds())

    session.add(observation)
    await session.commit()

    return {"message": "Observation marked as disappeared"}
