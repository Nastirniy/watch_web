from typing import List, Optional
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from loguru import logger
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload
from sqlalchemy import or_
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.database import get_session
from app.backend.models.observation import Observation, SignalType, Tag
from app.backend.models.user import User
from app.backend.models.post import Post
from app.backend.services.auth_context import AuthContext
from app.backend.services.permissions import require
from app.backend.services.access_control import AccessControlService
from app.backend.enums.general import Permissions, ObservationStatus
from app.backend.enums.routes import ObservationRoutes
from app.backend.helpers.audit_helper import create_audit_log
from app.backend.helpers.audit_helper import client_info
from app.backend.api.shared.models.request.observations_request import (
    ObservationCreate,
    ObservationUpdate,
    ObservationDisappeared,
    ObservationFrequencyCreate,
    ObservationFrequencyUpdate,
    ObservationFrequencyListParams,
    default_frequency_center,
)
from app.backend.api.shared.models.response.observations_response import (
    MessageResponse,
    ReportTypeChoice,
    ObservationWithMedia,
    ObservationFrequencyResponse,
)
from app.backend.api.shared.helpers.observation_helper import (
    get_observation_for_write,
    get_frequency_or_404,
    ensure_other_active_frequency,
    frequency_audit_values,
    observation_audit_values,
    frequency_rows,
)
from app.backend.filters.observation_frequency_filter import ObservationFrequencyFilter
from app.backend.filters.params.observation_params import ObservationFilterParams
from app.backend.filters.observation_filter import ObservationFilterService
from app.backend.filters.params.observation_report_params import ObservationReportParams, ObservationReportType
from app.backend.filters.observation_report_filter import ObservationReportFilter
from app.backend.services.observation_report_service import ObservationReportService
from app.backend.api.shared.models.response.search_response import EntitySchemaInfo
from app.backend.services.api_errors import ApiError, field_problem
from app.backend.enums.errors import ErrorCode, FieldErrorCode
from app.backend.api.shared.models.response.openapi_docs import file_response, header

router = APIRouter(prefix=ObservationRoutes.BASE.value, tags=[ObservationRoutes.tag()])


OBSERVATION_LOAD_OPTIONS = (
    selectinload(Observation.media_files),
    selectinload(Observation.tags),
    selectinload(Observation.user),
    selectinload(Observation.disappeared_by_user),
    selectinload(Observation.all_frequencies),
)


async def _load_observation(session: AsyncSession, observation_id: int) -> Observation:
    """Re-select with every relationship the response needs (refresh() does not load relationships)"""
    result = await session.exec(
        select(Observation).where(Observation.id == observation_id).options(*OBSERVATION_LOAD_OPTIONS)
    )
    return result.one()


async def _load_observation_response(session: AsyncSession, observation_id: int) -> ObservationWithMedia:
    return ObservationWithMedia.model_validate(await _load_observation(session, observation_id))


async def _validate_and_get_tags(session: AsyncSession, tag_ids: List[int], keep_ids: Optional[List[int]] = None) -> List[Tag]:
    """
    Validate that all tag IDs exist and are not deleted.
    Tags listed in keep_ids (already attached to the observation) may stay even if soft-deleted,
    so editing an observation never silently drops history.
    Returns a list of Tag instances ready to be associated with an observation.
    """
    condition = Tag.is_deleted == False
    if keep_ids:
        condition = or_(condition, Tag.id.in_(list(keep_ids)))
    result = await session.exec(
        select(Tag).where(Tag.id.in_(tag_ids), condition)
    )
    tags = list(result.all())

    found_ids = {t.id for t in tags}
    missing_ids = [tid for tid in tag_ids if tid not in found_ids]
    if missing_ids:
        raise ApiError(ErrorCode.TAGS_NOT_FOUND, tag_ids=missing_ids)

    return tags


@router.get(ObservationRoutes.LIST_ALL.value, response_model=List[ObservationWithMedia])
async def list_observations(
        filters: ObservationFilterParams = Depends(),
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.OBSERVATION_VIEW]))
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
    current_user = auth.user

    # Apply post access filter (enforced for regular users, optional for admins)
    AccessControlService.check_include_deleted(auth, filters, Permissions.OBSERVATION_ARCHIVE)
    filtered_post_ids = AccessControlService.resolve_post_scope(auth, filters.post_id)

    # Build base query and apply filters
    query = select(Observation).options(*OBSERVATION_LOAD_OPTIONS)
    query = ObservationFilterService.apply_filters(query, filters, filtered_post_ids)

    # Execute query
    result = await session.exec(query)
    observations = result.all()

    # Build response using Pydantic validation from attributes
    return [ObservationWithMedia.model_validate(obs) for obs in observations]


@router.get(ObservationRoutes.FILTER_SCHEMA.value, response_model=EntitySchemaInfo)
async def get_observation_filter_schema(
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.OBSERVATION_VIEW]))
):
    """Available filter params and sortable fields for observations."""
    current_user = auth.user
    user_is_admin = auth.is_admin
    return EntitySchemaInfo(**ObservationFilterService.get_schema(is_admin=user_is_admin))


@router.post(ObservationRoutes.CREATE.value, response_model=ObservationWithMedia)
async def create_observation(
        request: Request,
        observation_data: ObservationCreate,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.OBSERVATION_VIEW, Permissions.OBSERVATION_CREATE]))
):
    """
    Create new observation.
    Users can only create observations for their assigned post.
    Note: Field validation is handled by Pydantic model validators.
    """
    current_user = auth.user
    # Validate post exists and user has access
    post_result = await session.exec(
        select(Post).where(Post.id == observation_data.post_id)
    )
    post = post_result.first()
    if not post:
        raise ApiError(ErrorCode.POST_NOT_FOUND, post_id=observation_data.post_id)

    # Check if user has access to the post
    AccessControlService.check_post_write(auth, observation_data.post_id, "observation")

    # Validate signal_type exists and is active
    signal_type_result = await session.exec(
        select(SignalType).where(
            SignalType.id == observation_data.signal_type_id,
            SignalType.is_deleted == False
        )
    )
    signal_type = signal_type_result.first()
    if not signal_type:
        raise ApiError(ErrorCode.SIGNAL_TYPE_NOT_FOUND, signal_type_id=observation_data.signal_type_id)
    if not signal_type.is_active:
        raise ApiError(ErrorCode.SIGNAL_TYPE_INACTIVE, signal_type_name=signal_type.name)

    # Validate and get tags if provided
    tags = []
    if observation_data.tag_ids:
        tags = await _validate_and_get_tags(session, observation_data.tag_ids)

    observation = Observation(
        post_id=observation_data.post_id,
        signal_type_id=observation_data.signal_type_id,
        observed_at=observation_data.observed_at,
        duration_seconds=observation_data.duration_seconds,
        comment=observation_data.comment,
        confidence_level=observation_data.confidence_level,
        user_id=current_user.id,
        tags=tags,
        all_frequencies=frequency_rows(observation_data.frequencies),
    )

    session.add(observation)

    # Update user's last record created
    current_user.last_record_created_at = datetime.now(timezone.utc)
    session.add(current_user)

    try:
        await session.commit()
        observation = await _load_observation(session, observation.id)

        # Create audit log
        await create_audit_log(
            session=session,
            action="create",
            resource_type="observation",
            resource_id=observation.id,
            user_id=current_user.id,
            new_values=observation_audit_values(observation),
            details={"message": f"User {current_user.username} created new observation #{observation.id}"},
            **client_info(request, auth)
        )
        await session.commit()

        return await _load_observation_response(session, observation.id)
    except IntegrityError as e:
        await session.rollback()
        # This should rarely happen since we pre-validate foreign keys
        # but kept as safety net for unexpected constraints
        logger.exception(f"Unexpected database constraint violation: {str(e.orig) if hasattr(e, 'orig') else str(e)}")  #TODO: make it as loggers config
        raise ApiError(ErrorCode.DATABASE_CONSTRAINT_VIOLATION)


@router.put(ObservationRoutes.OBSERVATION_BY_ID.value, response_model=ObservationWithMedia)
async def update_observation(
        request: Request,
        observation_id: int,
        observation_data: ObservationUpdate,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.OBSERVATION_VIEW, Permissions.OBSERVATION_UPDATE]))
):
    """
    Update existing observation.
    Users can only update observations from their assigned post.
    Archived observations answer 404 until restored (POST /{observation_id}/restore).
    """
    current_user = auth.user
    # Get observation by ID with tags and bands pre-loaded
    query = select(Observation).where(
        Observation.id == observation_id
    ).options(selectinload(Observation.tags), selectinload(Observation.all_frequencies))
    
    result = await session.exec(query)
    observation = result.first()

    if not observation:
        raise ApiError(ErrorCode.OBSERVATION_NOT_FOUND)

    # Store old values for auditing
    old_values = observation_audit_values(observation)

    # Check if user has access to this observation's post
    AccessControlService.check_post_write(auth, observation.post_id, "observation")
    AccessControlService.check_observation_ownership(auth, observation)

    if observation.is_deleted:
        raise ApiError(ErrorCode.OBSERVATION_NOT_FOUND)

    if observation_data.signal_type_id is not None:
        signal_type_result = await session.exec(
            select(SignalType).where(
                SignalType.id == observation_data.signal_type_id,
                SignalType.is_deleted == False
            )
        )
        signal_type = signal_type_result.first()
        if not signal_type:
            raise ApiError(ErrorCode.SIGNAL_TYPE_NOT_FOUND, signal_type_id=observation_data.signal_type_id)
        if not signal_type.is_active:
            raise ApiError(ErrorCode.SIGNAL_TYPE_INACTIVE, signal_type_name=signal_type.name)

    update_data = observation_data.model_dump(exclude_unset=True, exclude={"tag_ids"})
    for field, value in update_data.items():
        setattr(observation, field, value)

    observation.updated_at = datetime.now(timezone.utc)

    # Update tags if provided in request (including null/empty list to clear)
    # Using model_dump(exclude_unset=True) to check if key was actually in request
    if "tag_ids" in observation_data.model_dump(exclude_unset=True):
        if observation_data.tag_ids:
            tags = await _validate_and_get_tags(session, observation_data.tag_ids, keep_ids=[t.id for t in observation.tags])
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
            old_values=old_values,
            new_values=observation_audit_values(observation),
            details={"message": f"User {current_user.username} updated observation #{observation.id}"},
            **client_info(request, auth)
        )
        await session.commit()

        return await _load_observation_response(session, observation.id)
    except IntegrityError as e:
        await session.rollback()
        # This should rarely happen since we pre-validate foreign keys
        # but kept as safety net for unexpected constraints
        logger.exception(
            f"Unexpected database constraint violation: {str(e.orig) if hasattr(e, 'orig') else str(e)}")  # TODO: make it as loggers config
        raise ApiError(ErrorCode.DATABASE_CONSTRAINT_VIOLATION)


@router.post(ObservationRoutes.MARK_DISAPPEARED.value, response_model=MessageResponse)
async def mark_observation_disappeared(
        request: Request,
        observation_id: int,
        disappeared_data: ObservationDisappeared,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.OBSERVATION_VIEW, Permissions.OBSERVATION_UPDATE]))
):
    """
    Mark observation as disappeared.
    Users can only mark observations from their assigned post.
    """
    current_user = auth.user
    result = await session.exec(
        select(Observation).where(
            Observation.id == observation_id,
            Observation.is_deleted == False
        )
    )
    observation = result.first()

    if not observation:
        raise ApiError(ErrorCode.OBSERVATION_NOT_FOUND)

    # Store old values for auditing
    old_values = observation.model_dump()

    # Check if user has access to this observation's post
    AccessControlService.check_post_write(auth, observation.post_id, "observation")
    AccessControlService.check_observation_ownership(auth, observation)

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
        old_values=old_values,
        new_values=observation.model_dump(),
        details={"message": f"User {current_user.username} marked observation #{observation.id} as disappeared"},
        **client_info(request, auth)
    )
    
    await session.commit()

    return MessageResponse(message="Observation marked as disappeared")


@router.get(ObservationRoutes.REPORT_TYPES.value, response_model=List[ReportTypeChoice])
async def get_report_types(
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.OBSERVATION_VIEW]))
):
    """
    Returns available observation report types for web UI select/dropdown.
    Each entry: {value, label (Ukrainian), description (Ukrainian)}
    """
    return ObservationReportType.choices()


@router.post(
    ObservationRoutes.REPORT.value,
    response_class=Response,
    responses=file_response("Excel report", ["application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"],
                            {"Content-Disposition": header('attachment; filename="<report>.xlsx"')}),
)
async def generate_observation_report(
        params: ObservationReportParams,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.OBSERVATION_VIEW, Permissions.EXPORT_CREATE], admin=True))
):
    """
    Generate an Excel (.xlsx) report for observations. Admin only.

    The report header contains the filter criteria used (in Ukrainian).
    One row per frequency band (rc_freq_part: rc bands, video_freq_part: video bands, all_fields: every band);
    the observation columns repeat for each of its bands. Bands of archived observations and archived bands
    appear only with include_deleted. Posts outside the caller's scope are never included.
    all_fields columns: user, post, signal type, band kind, emitter group, frequency min/max/center, bandwidth,
    observed time, disappeared time, duration (minutes, one decimal), comment, creation date, tags.
    All datetimes are displayed in Kyiv local time (EET/EEST).

    Filter combinations supported:
    - By specific observation IDs
    - By date range (observed_at or created_at)
    - By user IDs (one or many)
    - By post IDs (one or many)
    - By signal type IDs
    - By statuses
    - Any combination of the above
    """
    AccessControlService.check_include_deleted(auth, params, Permissions.OBSERVATION_ARCHIVE)
    for post_id in params.post_ids or []:
        AccessControlService.check_post_view(auth, post_id)
    query = ObservationReportFilter.build_query(params, AccessControlService.post_scope(auth))

    result = await session.exec(query)
    frequencies = list(result.all())

    tag_names = None
    if params.tag_ids:
        tag_rows = await session.exec(select(Tag.id, Tag.name).where(Tag.id.in_(params.tag_ids)))
        tag_names = {tag_id: name for tag_id, name in tag_rows.all()}
    excel_bytes = ObservationReportService.generate_excel(frequencies, params, tag_names=tag_names)

    filename = f"observations_report_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.xlsx"
    return Response(
        content=excel_bytes,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def _set_observation_archived(
        request: Request,
        session: AsyncSession,
        auth: AuthContext,
        observation_id: int,
        archived: bool,
) -> ObservationWithMedia:
    """Shared body of archive / restore: ownership rules, flag flip, audit row."""
    current_user = auth.user
    result = await session.exec(select(Observation).where(Observation.id == observation_id))
    observation = result.first()
    if not observation or (archived and observation.is_deleted):
        raise ApiError(ErrorCode.OBSERVATION_NOT_FOUND)
    if not archived and not observation.is_deleted:
        raise ApiError(ErrorCode.OBSERVATION_NOT_ARCHIVED)

    AccessControlService.check_post_write(auth, observation.post_id, "observation")
    AccessControlService.check_observation_ownership(auth, observation)

    action = "archive" if archived else "restore"
    old_values = observation.model_dump()
    observation.is_deleted = archived
    observation.updated_at = datetime.now(timezone.utc)
    session.add(observation)
    await session.commit()

    await create_audit_log(
        session=session,
        action=action,
        resource_type="observation",
        resource_id=observation.id,
        user_id=current_user.id,
        old_values=old_values,
        new_values=observation.model_dump(),
        details={"message": f"User {current_user.username} {action}d observation #{observation.id}"},
        **client_info(request, auth)
    )
    await session.commit()
    logger.info(f"User {current_user.username} {action}d observation {observation.id}")
    return await _load_observation_response(session, observation.id)


@router.post(ObservationRoutes.OBSERVATION_ARCHIVE.value, response_model=ObservationWithMedia)
async def archive_observation(
        request: Request,
        observation_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.OBSERVATION_VIEW, Permissions.OBSERVATION_ARCHIVE]))
):
    """Archive an observation (is_deleted=true). Owner or observation.manage_any, write access to its post.

    Nothing is removed: media files stay attached, the row is hidden from lists unless include_deleted=true.
    Undo with POST /{observation_id}/restore. Already archived or unknown: 404.
    """
    return await _set_observation_archived(request, session, auth, observation_id, archived=True)


@router.post(ObservationRoutes.OBSERVATION_RESTORE.value, response_model=ObservationWithMedia)
async def restore_observation(
        request: Request,
        observation_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.OBSERVATION_VIEW, Permissions.OBSERVATION_ARCHIVE]))
):
    """Restore an archived observation (is_deleted=false). Same rules as archive. Not archived: 409."""
    return await _set_observation_archived(request, session, auth, observation_id, archived=False)


# =============================================================================
# FREQUENCY BANDS
# =============================================================================
async def _audit_frequency(
        request: Request,
        session: AsyncSession,
        auth: AuthContext,
        action: str,
        frequency,
        old_values: Optional[dict] = None,
) -> None:
    await create_audit_log(
        session=session,
        action=action,
        resource_type="observation_frequency",
        resource_id=frequency.id,
        user_id=auth.user.id,
        old_values=old_values,
        new_values=frequency_audit_values(frequency),
        details={
            "observation_id": frequency.observation_id,
            "message": f"User {auth.user.username} {action}d frequency #{frequency.id} of observation #{frequency.observation_id}",
        },
        **client_info(request, auth)
    )


@router.get(ObservationRoutes.OBSERVATION_FREQUENCIES.value, response_model=List[ObservationFrequencyResponse])
async def list_observation_frequencies(
        observation_id: int,
        params: ObservationFrequencyListParams = Depends(),
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.OBSERVATION_VIEW]))
):
    """Bands of one observation. Archived bands and bands of an archived observation need include_deleted=true."""
    AccessControlService.check_include_deleted(auth, params, Permissions.OBSERVATION_ARCHIVE)
    observation = (await session.exec(select(Observation).where(Observation.id == observation_id))).first()
    if not observation:
        raise ApiError(ErrorCode.OBSERVATION_NOT_FOUND)
    AccessControlService.check_post_view(auth, observation.post_id, "observation")
    if observation.is_deleted and not params.include_deleted:
        raise ApiError(ErrorCode.OBSERVATION_NOT_FOUND)

    result = await session.exec(ObservationFrequencyFilter.of_observation(observation_id, params.include_deleted))
    return [ObservationFrequencyResponse.model_validate(f) for f in result.all()]


@router.post(ObservationRoutes.OBSERVATION_FREQUENCIES.value, response_model=ObservationFrequencyResponse)
async def add_observation_frequency(
        request: Request,
        observation_id: int,
        frequency_data: ObservationFrequencyCreate,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.OBSERVATION_VIEW, Permissions.OBSERVATION_UPDATE]))
):
    """Add a band to an observation. Owner or observation.manage_any, write access to its post.
    Archived or unknown observation: 404."""
    observation = await get_observation_for_write(session, auth, observation_id, lock=True)

    frequency = frequency_rows([frequency_data])[0]
    observation.all_frequencies.append(frequency)
    observation.updated_at = datetime.now(timezone.utc)
    session.add(observation)
    await session.commit()

    await _audit_frequency(request, session, auth, "create", frequency)
    await session.commit()
    return ObservationFrequencyResponse.model_validate(frequency)


@router.put(ObservationRoutes.OBSERVATION_FREQUENCY_BY_ID.value, response_model=ObservationFrequencyResponse)
async def update_observation_frequency(
        request: Request,
        observation_id: int,
        frequency_id: int,
        frequency_data: ObservationFrequencyUpdate,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.OBSERVATION_VIEW, Permissions.OBSERVATION_UPDATE]))
):
    """Change fields of one band. Same access rules as adding one. Archived band or observation: 404.
    When frequency_min or frequency_max changes and frequency_center is not sent, the center is recalculated."""
    observation = await get_observation_for_write(session, auth, observation_id, lock=True)
    frequency = get_frequency_or_404(observation, frequency_id)
    if frequency.is_deleted:
        raise ApiError(ErrorCode.FREQUENCY_NOT_FOUND)

    changes = frequency_data.model_dump(exclude_unset=True)
    frequency_min = changes.get("frequency_min", frequency.frequency_min)
    frequency_max = changes.get("frequency_max", frequency.frequency_max)
    if frequency_min >= frequency_max:
        raise field_problem(FieldErrorCode.FREQUENCY_RANGE_INVALID, ("body", "frequency_max"))
    if "kind" in changes:
        changes["kind"] = changes["kind"].value
    if ("frequency_min" in changes or "frequency_max" in changes) and "frequency_center" not in changes:
        changes["frequency_center"] = default_frequency_center(frequency_min, frequency_max)

    old_values = frequency_audit_values(frequency)
    now = datetime.now(timezone.utc)
    for field, value in changes.items():
        setattr(frequency, field, value)
    frequency.updated_at = now
    observation.updated_at = now
    session.add(observation)
    await session.commit()

    await _audit_frequency(request, session, auth, "update", frequency, old_values=old_values)
    await session.commit()
    return ObservationFrequencyResponse.model_validate(frequency)


async def _set_frequency_archived(
        request: Request,
        session: AsyncSession,
        auth: AuthContext,
        observation_id: int,
        frequency_id: int,
        archived: bool,
) -> ObservationFrequencyResponse:
    """Shared body of band archive / restore"""
    observation = await get_observation_for_write(session, auth, observation_id, lock=True)
    frequency = get_frequency_or_404(observation, frequency_id)
    if archived and frequency.is_deleted:
        raise ApiError(ErrorCode.FREQUENCY_NOT_FOUND)
    if not archived and not frequency.is_deleted:
        raise ApiError(ErrorCode.FREQUENCY_NOT_ARCHIVED)
    if archived:
        ensure_other_active_frequency(observation, frequency.id)

    action = "archive" if archived else "restore"
    old_values = frequency_audit_values(frequency)
    now = datetime.now(timezone.utc)
    frequency.is_deleted = archived
    frequency.updated_at = now
    observation.updated_at = now
    session.add(observation)
    await session.commit()

    await _audit_frequency(request, session, auth, action, frequency, old_values=old_values)
    await session.commit()
    logger.info(f"User {auth.user.username} {action}d frequency {frequency.id} of observation {observation.id}")
    return ObservationFrequencyResponse.model_validate(frequency)


@router.post(ObservationRoutes.OBSERVATION_FREQUENCY_ARCHIVE.value, response_model=ObservationFrequencyResponse)
async def archive_observation_frequency(
        request: Request,
        observation_id: int,
        frequency_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.OBSERVATION_VIEW, Permissions.OBSERVATION_ARCHIVE]))
):
    """Archive a band (is_deleted=true): it leaves the observation response, lists, filters and reports.
    Owner or observation.manage_any, write access to the post. Already archived, unknown, or archived
    observation: 404. The last active band of an observation: 409."""
    return await _set_frequency_archived(request, session, auth, observation_id, frequency_id, archived=True)


@router.post(ObservationRoutes.OBSERVATION_FREQUENCY_RESTORE.value, response_model=ObservationFrequencyResponse)
async def restore_observation_frequency(
        request: Request,
        observation_id: int,
        frequency_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.OBSERVATION_VIEW, Permissions.OBSERVATION_ARCHIVE]))
):
    """Restore an archived band. Same rules as archive. Not archived: 409."""
    return await _set_frequency_archived(request, session, auth, observation_id, frequency_id, archived=False)
