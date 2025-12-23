from typing import List, Optional
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, status as fapi_status
from loguru import logger
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.database import get_session
from app.backend.models.observation import Observation, SignalType
from app.backend.models.user import User
from app.backend.models.post import Post
from app.backend.dependencies import get_current_user
from app.backend.services.permissions import PermissionService
from app.backend.services.access_control import AccessControlService
from app.backend.enums.general import Permissions, ObservationStatus
from app.backend.enums.routes import ObservationRoutes
from app.backend.routes.models.request.observations_request import (
    ObservationCreate,
    ObservationUpdate,
    ObservationDisappeared
)
from app.backend.routes.models.response.observations_response import MessageResponse, ObservationWithMedia

router = APIRouter()


@router.get(ObservationRoutes.LIST_ALL.value, response_model=List[ObservationWithMedia])
@PermissionService.require_permission(Permissions.OBSERVATION_VIEW.value)
async def list_observations(
        skip: int = 0,
        limit: int = 100,
        post_id: Optional[int] = None,
        status: Optional[str] = None,
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
    """
    List observations with attached media IDs.
    Regular users can only see observations from their assigned post.
    Admins can see all observations or filter by specific post.

    DB Load: Uses eager loading (selectinload) to fetch media_files in a single additional query,
    preventing N+1 query problem. For N observations, this executes 2 queries instead of N+1.
    """

    # Apply post access filter
    filtered_post_id = await AccessControlService.filter_query_by_post_access(
        current_user, session, post_id
    )

    query = select(Observation).where(Observation.is_deleted == False)

    # Apply post filter (enforced for regular users, optional for admins)
    if filtered_post_id:
        query = query.where(Observation.post_id == filtered_post_id)

    if status:
        query = query.where(Observation.status == status)

    # Eager load media_files to avoid N+1 queries
    query = query.options(selectinload(Observation.media_files))

    result = await session.exec(query.offset(skip).limit(limit))
    observations = result.all()

    # Build response with media IDs
    response = []
    for obs in observations:
        obs_dict = {
            "id": obs.id,
            "user_id": obs.user_id,
            "post_id": obs.post_id,
            "signal_type_id": obs.signal_type_id,
            "frequency_min": obs.frequency_min,
            "frequency_max": obs.frequency_max,
            "frequency_center": obs.frequency_center,
            "bandwidth": obs.bandwidth,
            "video_frequency_min": obs.video_frequency_min,
            "video_frequency_max": obs.video_frequency_max,
            "video_frequency_center": obs.video_frequency_center,
            "video_bandwidth": obs.video_bandwidth,
            "observed_at": obs.observed_at,
            "duration_seconds": obs.duration_seconds,
            "comment": obs.comment,
            "status": obs.status,
            "confidence_level": obs.confidence_level,
            "is_deleted": obs.is_deleted,
            "created_at": obs.created_at,
            "updated_at": obs.updated_at,
            "disappeared_at": obs.disappeared_at,
            "disappeared_by": obs.disappeared_by,
            "media_ids": [media.id for media in obs.media_files]
        }
        response.append(ObservationWithMedia(**obs_dict))

    return response


@router.post(ObservationRoutes.CREATE.value, response_model=Observation)
@PermissionService.require_permission(Permissions.OBSERVATION_CREATE.value)
async def create_observation(
        observation_data: ObservationCreate,
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
    """
    Create new observation.
    Users can only create observations for their assigned post.
    Note: Field validation is handled by Pydantic model validators.
    """
    # Validate post exists and user has access
    post_result = await session.exec(
        select(Post).where(Post.id == observation_data.post_id)
    )
    post = post_result.first()
    if not post:
        raise HTTPException(
            status_code=fapi_status.HTTP_404_NOT_FOUND,
            detail=f"Post with id {observation_data.post_id} does not exist"
        )

    # Check if user has access to the post
    await AccessControlService.check_post_membership(
        current_user,
        observation_data.post_id,
        session,
        f"{Permissions.OBSERVATION_CREATE.name} for {ObservationRoutes.BASE.value}"
    )

    # Validate signal_type exists and is active
    signal_type_result = await session.exec(
        select(SignalType).where(
            SignalType.id == observation_data.signal_type_id,
            SignalType.is_deleted == False
        )
    )
    signal_type = signal_type_result.first()
    if not signal_type:
        raise HTTPException(
            status_code=fapi_status.HTTP_404_NOT_FOUND,
            detail=f"SignalType with id {observation_data.signal_type_id} does not exist"
        )
    if not signal_type.is_active:
        raise HTTPException(
            status_code=fapi_status.HTTP_400_BAD_REQUEST,
            detail=f"SignalType '{signal_type.name}' is inactive and cannot be used"
        )

    has_default_freq = observation_data.frequency_min is not None and observation_data.frequency_max is not None
    has_video_freq = observation_data.video_frequency_min is not None and observation_data.video_frequency_max is not None

    observation = Observation(
        post_id=observation_data.post_id,
        signal_type_id=observation_data.signal_type_id,
        frequency_min=observation_data.frequency_min,
        frequency_max=observation_data.frequency_max,
        frequency_center=(observation_data.frequency_min + observation_data.frequency_max) / 2 if has_default_freq else None,
        bandwidth=observation_data.frequency_max - observation_data.frequency_min if has_default_freq else None,
        video_frequency_min=observation_data.video_frequency_min,
        video_frequency_max=observation_data.video_frequency_max,
        video_frequency_center=(observation_data.video_frequency_min + observation_data.video_frequency_max) / 2 if has_video_freq else None,
        video_bandwidth=observation_data.video_frequency_max - observation_data.video_frequency_min if has_video_freq else None,
        observed_at=observation_data.observed_at,
        duration_seconds=observation_data.duration_seconds,
        comment=observation_data.comment,
        confidence_level=observation_data.confidence_level,
        user_id=current_user.id
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
        # This should rarely happen since we pre-validate foreign keys
        # but kept as safety net for unexpected constraints
        logger.exception(f"Unexpected database constraint violation: {str(e.orig) if hasattr(e, 'orig') else str(e)}")  #TODO: make it as loggers config
        raise HTTPException(
            status_code=fapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Unexpected database constraint violation"
        )


@router.put(ObservationRoutes.OBSERVATION_BY_ID.value, response_model=Observation)
@PermissionService.require_permission(Permissions.OBSERVATION_UPDATE.value)
async def update_observation(
        observation_id: int,
        observation_data: ObservationUpdate,
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
    """
    Update existing observation.
    Users can only update observations from their assigned post.
    """
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

    # Check if user has access to this observation's post
    await AccessControlService.check_post_membership(
        current_user,
        observation.post_id,
        session,
        f"{Permissions.OBSERVATION_UPDATE.name} for {ObservationRoutes.BASE.value}"
    )

    if observation_data.signal_type_id is not None:
        signal_type_result = await session.exec(
            select(SignalType).where(
                SignalType.id == observation_data.signal_type_id,
                SignalType.is_deleted == False
            )
        )
        signal_type = signal_type_result.first()
        if not signal_type:
            raise HTTPException(
                status_code=fapi_status.HTTP_404_NOT_FOUND,
                detail=f"SignalType with id {observation_data.signal_type_id} does not exist"
            )
        if not signal_type.is_active:
            raise HTTPException(
                status_code=fapi_status.HTTP_400_BAD_REQUEST,
                detail=f"SignalType '{signal_type.name}' is inactive and cannot be used"
            )

    update_data = observation_data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(observation, field, value)

    # Validate that after update, at least one set of frequencies exists
    has_default_freq = observation.frequency_min is not None and observation.frequency_max is not None
    has_video_freq = observation.video_frequency_min is not None and observation.video_frequency_max is not None

    if not has_default_freq and not has_video_freq:
        raise HTTPException(
            status_code=fapi_status.HTTP_400_BAD_REQUEST,
            detail="At least one set of frequencies must exist: either (frequency_min, frequency_max) or (video_frequency_min, video_frequency_max)"
        )

    # Recalculate center frequency and bandwidth if min/max changed
    if observation_data.frequency_min is not None or observation_data.frequency_max is not None:
        if observation.frequency_min is not None and observation.frequency_max is not None:
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
        # This should rarely happen since we pre-validate foreign keys
        # but kept as safety net for unexpected constraints
        logger.exception(
            f"Unexpected database constraint violation: {str(e.orig) if hasattr(e, 'orig') else str(e)}")  # TODO: make it as loggers config
        raise HTTPException(
            status_code=fapi_status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Unexpected database constraint violation"
        )


@router.post(ObservationRoutes.MARK_DISAPPEARED.value, response_model=MessageResponse)
@PermissionService.require_permission(Permissions.OBSERVATION_UPDATE.value)
async def mark_observation_disappeared(
        observation_id: int,
        disappeared_data: ObservationDisappeared,
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
    """
    Mark observation as disappeared.
    Users can only mark observations from their assigned post.
    """
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

    # Check if user has access to this observation's post
    await AccessControlService.check_post_membership(
        current_user,
        observation.post_id,
        session,
        f"{Permissions.OBSERVATION_UPDATE.name} for {ObservationRoutes.MARK_DISAPPEARED.value}"
    )

    observation.status = ObservationStatus.DISAPPEARED.value
    observation.disappeared_at = disappeared_data.disappeared_at
    observation.disappeared_by = current_user.id
    observation.updated_at = datetime.now(timezone.utc)

    if observation.observed_at:
        observed_at = observation.observed_at if observation.observed_at.tzinfo else observation.observed_at.replace(tzinfo=timezone.utc)
        time_delta = disappeared_data.disappeared_at - observed_at
        observation.duration_seconds = int(time_delta.total_seconds())

    session.add(observation)
    await session.commit()

    return MessageResponse(message="Observation marked as disappeared")
