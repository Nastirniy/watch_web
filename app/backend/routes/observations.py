from typing import List, Optional
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, status as fapi_status, Request
from loguru import logger
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.database import get_session
from app.backend.models.observation import Observation, SignalType, Tag
from app.backend.models.user import User
from app.backend.models.post import Post
from app.backend.dependencies import get_current_user
from app.backend.services.permissions import PermissionService
from app.backend.services.access_control import AccessControlService
from app.backend.enums.general import Permissions, ObservationStatus
from app.backend.enums.routes import ObservationRoutes
from app.backend.helpers.audit_helper import create_audit_log
from app.backend.routes.models.request.observations_request import (
    ObservationCreate,
    ObservationUpdate,
    ObservationDisappeared,
)
from app.backend.routes.models.response.observations_response import MessageResponse, ObservationWithMedia
from app.backend.filters.params.observation_params import ObservationFilterParams
from app.backend.filters.observation_filter import ObservationFilterService
from app.backend.routes.models.response.search_response import EntitySchemaInfo

router = APIRouter()


async def _validate_and_get_tags(session: AsyncSession, tag_ids: List[int]) -> List[Tag]:
    """
    Validate that all tag IDs exist and are not deleted.
    Returns a list of Tag instances ready to be associated with an observation.
    """
    result = await session.exec(
        select(Tag).where(Tag.id.in_(tag_ids), Tag.is_deleted == False)
    )
    tags = list(result.all())

    found_ids = {t.id for t in tags}
    missing_ids = [tid for tid in tag_ids if tid not in found_ids]
    if missing_ids:
        raise HTTPException(
            status_code=fapi_status.HTTP_404_NOT_FOUND,
            detail=f"Tags not found: {missing_ids}"
        )

    return tags


@router.get(ObservationRoutes.LIST_ALL.value, response_model=List[ObservationWithMedia])
@PermissionService.require_permission(Permissions.OBSERVATION_VIEW.value)
async def list_observations(
        filters: ObservationFilterParams = Depends(),
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
    """
    List observations with attached media IDs and comprehensive filtering.
    Regular users can only see observations from their assigned post.
    Admins can see all observations or filter by specific post.

    Filters (all optional):
    - Basic: post_id, status
    - Absolute dates: observed_at_from, observed_at_to
    - Relative dates: last_n_days (e.g., 3 = last 3 days including today)
                      day_offset (e.g., 0=today, 1=yesterday, 2=day before)
    - skip_empty_days: Skip days with no observations (for relative date queries)
    - Pagination: skip, limit (if not provided, returns all matching records)

    Examples:
    - Get all observations: /observations
    - Get last 7 days: /observations?last_n_days=7
    - Get yesterday's data: /observations?day_offset=1
    - Get specific date range: /observations?observed_at_from=2025-01-01&observed_at_to=2025-01-31
    - Paginated: /observations?skip=0&limit=100

    DB Load: Uses eager loading (selectinload) to fetch media_files in a single additional query,
    preventing N+1 query problem. For N observations, this executes 2 queries instead of N+1.
    """

    # Apply post access filter (enforced for regular users, optional for admins)
    filtered_post_id = await AccessControlService.filter_query_by_post_access(
        current_user, session, filters.post_id
    )

    # Build base query and apply filters
    query = select(Observation).options(
        selectinload(Observation.media_files),
        selectinload(Observation.tags)
    )
    query = ObservationFilterService.apply_filters(query, filters, filtered_post_id)

    # Execute query
    result = await session.exec(query)
    observations = result.all()

    # Build response using Pydantic validation from attributes
    return [ObservationWithMedia.model_validate(obs) for obs in observations]


@router.get(ObservationRoutes.FILTER_SCHEMA.value, response_model=EntitySchemaInfo)
@PermissionService.require_permission(Permissions.OBSERVATION_VIEW.value)
async def get_observation_filter_schema(
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
    """Available filter params and sortable fields for observations."""
    user_is_admin = await PermissionService.has_permission(
        session, current_user, Permissions.IS_ADMIN.value
    )
    return EntitySchemaInfo(**ObservationFilterService.get_schema(is_admin=user_is_admin))


@router.post(ObservationRoutes.CREATE.value, response_model=Observation)
@PermissionService.require_permission(Permissions.OBSERVATION_CREATE.value)
async def create_observation(
        request: Request,
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

    # Validate and get tags if provided
    tags = []
    if observation_data.tag_ids:
        tags = await _validate_and_get_tags(session, observation_data.tag_ids)

    # Calculate derived frequency fields if not explicitly provided
    freq_center = observation_data.frequency_center
    bandwidth = None
    if has_default_freq:
        if freq_center is None:
            freq_center = (observation_data.frequency_min + observation_data.frequency_max) / 2
        bandwidth = observation_data.frequency_max - observation_data.frequency_min

    video_freq_center = observation_data.video_frequency_center
    video_bandwidth = None
    if has_video_freq:
        if video_freq_center is None:
            video_freq_center = (observation_data.video_frequency_min + observation_data.video_frequency_max) / 2
        video_bandwidth = observation_data.video_frequency_max - observation_data.video_frequency_min

    observation = Observation(
        post_id=observation_data.post_id,
        signal_type_id=observation_data.signal_type_id,
        frequency_min=observation_data.frequency_min,
        frequency_max=observation_data.frequency_max,
        frequency_center=freq_center,
        bandwidth=bandwidth,
        video_frequency_min=observation_data.video_frequency_min,
        video_frequency_max=observation_data.video_frequency_max,
        video_frequency_center=video_freq_center,
        video_bandwidth=video_bandwidth,
        observed_at=observation_data.observed_at,
        duration_seconds=observation_data.duration_seconds,
        comment=observation_data.comment,
        confidence_level=observation_data.confidence_level,
        user_id=current_user.id,
        tags=tags
    )

    session.add(observation)

    # Update user's last record created
    current_user.last_record_created_at = datetime.now(timezone.utc)
    session.add(current_user)

    try:
        await session.commit()
        await session.refresh(observation)

        # Create audit log
        await create_audit_log(
            session=session,
            action="create",
            resource_type="observation",
            resource_id=observation.id,
            user_id=current_user.id,
            session_id=getattr(request.state, "session_id", None),
            new_values=observation.model_dump(),
            details={"message": f"User {current_user.username} created new observation #{observation.id}"},
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_url=str(request.url)
        )
        await session.commit()

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
        request: Request,
        observation_id: int,
        observation_data: ObservationUpdate,
        session: AsyncSession = Depends(get_session),
        current_user: User = Depends(get_current_user)
):
    """
    Update existing observation.
    Users can only update observations from their assigned post.
    """
    # Get observation by ID with tags pre-loaded
    query = select(Observation).where(
        Observation.id == observation_id,
        Observation.is_deleted == False
    ).options(selectinload(Observation.tags))
    
    result = await session.exec(query)
    observation = result.first()

    if not observation:
        raise HTTPException(
            status_code=fapi_status.HTTP_404_NOT_FOUND,
            detail="Observation not found"
        )

    # Store old values for auditing
    old_values = observation.model_dump()

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

    update_data = observation_data.model_dump(exclude_unset=True, exclude={"tag_ids"})
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

    # Recalculate or cleanup derived frequency fields
    if observation.frequency_min is not None and observation.frequency_max is not None:
        observation.bandwidth = observation.frequency_max - observation.frequency_min
        if "frequency_center" not in update_data:
            observation.frequency_center = (observation.frequency_min + observation.frequency_max) / 2
    else:
        observation.bandwidth = None
        if "frequency_center" not in update_data:
            observation.frequency_center = None

    if observation.video_frequency_min is not None and observation.video_frequency_max is not None:
        observation.video_bandwidth = observation.video_frequency_max - observation.video_frequency_min
        if "video_frequency_center" not in update_data:
            observation.video_frequency_center = (observation.video_frequency_min + observation.video_frequency_max) / 2
    else:
        observation.video_bandwidth = None
        if "video_frequency_center" not in update_data:
            observation.video_frequency_center = None

    observation.updated_at = datetime.now(timezone.utc)

    # Update tags if provided in request (including null/empty list to clear)
    # Using model_dump(exclude_unset=True) to check if key was actually in request
    if "tag_ids" in observation_data.model_dump(exclude_unset=True):
        if observation_data.tag_ids:
            tags = await _validate_and_get_tags(session, observation_data.tag_ids)
            observation.tags = tags
        else:
            observation.tags = []

    session.add(observation)

    try:
        await session.commit()
        await session.refresh(observation)

        # Create audit log
        await create_audit_log(
            session=session,
            action="update",
            resource_type="observation",
            resource_id=observation.id,
            user_id=current_user.id,
            session_id=getattr(request.state, "session_id", None),
            old_values=old_values,
            new_values=observation.model_dump(),
            details={"message": f"User {current_user.username} updated observation #{observation.id}"},
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_url=str(request.url)
        )
        await session.commit()

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
        request: Request,
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

    # Store old values for auditing
    old_values = observation.model_dump()

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
    
    # Create audit log
    await create_audit_log(
        session=session,
        action="update",
        resource_type="observation",
        resource_id=observation.id,
        user_id=current_user.id,
        session_id=getattr(request.state, "session_id", None),
        old_values=old_values,
        new_values=observation.model_dump(),
        details={"message": f"User {current_user.username} marked observation #{observation.id} as disappeared"},
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        request_url=str(request.url)
    )
    
    await session.commit()

    return MessageResponse(message="Observation marked as disappeared")
