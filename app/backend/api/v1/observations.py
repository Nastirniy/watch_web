from typing import List, Optional, Annotated
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, Request, Query
from fastapi.responses import Response
from loguru import logger
from sqlalchemy.orm import selectinload
from sqlalchemy import or_
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.database import get_session
from app.backend.utils.row_locks import lock_first
from app.backend.models.observation import Observation, SignalType, Tag
from app.backend.models.post import Post
from app.backend.auth.context import AuthContext
from app.backend.auth.require import require
from app.backend.auth.access_control import AccessControlService
from app.backend.enums.general import Permissions, ObservationStatus, ObservationReportType
from app.backend.enums.routes import ObservationRoutes
from app.backend.api.shared.helpers.audit_helper import client_info
from app.backend.services.scope_groups import UNIT_KINDS
from app.backend.services.units import (observation_unit_labels, observation_unit_rows, observation_units_any,
                                        observation_units_filter, unit_labels, visible_unit_ids)
from app.backend.services.audit import create_audit_log
from app.backend.api.v1.models.request.observations import (
    ObservationCreate,
    ObservationUpdate,
    ObservationDisappeared,
    ObservationFrequencyCreate,
    ObservationFrequencyUpdate,
    ObservationReportParams,
    default_frequency_center,
)
from app.backend.api.v1.models.response.observations import (
    MessageResponse,
    ReportTypeChoice,
    ObservationWithMedia, observations_to_response,
    ObservationFrequencyResponse,
)
from app.backend.api.shared.helpers.post_helper import ensure_post_not_archived, refuse_archived_post
from app.backend.api.shared.helpers.observation_helper import (
    get_observation_for_write,
    get_frequency_or_404,
    disappeared_duration,
    ensure_other_active_frequency,
    frequency_audit_values,
    observation_audit_values,
    frequency_rows,
)
from app.backend.filters.observation_frequency_filter import ObservationFrequencyFilter
from app.backend.api.v1.models.query.observations import ObservationFilterParams, ObservationFrequencyListParams
from app.backend.filters.observation_filter import ObservationFilterService
from app.backend.filters.observation_report_filter import ObservationReportFilter
from app.backend.services.observation_report_service import ObservationReportService
from app.backend.errors.problems import ApiError, field_problem
from app.backend.enums.errors import AccessErrorCode, ErrorCode, FieldErrorCode
from app.backend.api.shared.openapi_docs import file_response, header
from app.backend.errors.openapi import GUARDED, problems

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
    return (await observations_to_response(session, [await _load_observation(session, observation_id)]))[0]


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


@router.get(
    ObservationRoutes.LIST_ALL.value,
    response_model=List[ObservationWithMedia],
    responses=problems(
        AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION,
        AccessErrorCode.POST_ACCESS_DENIED, *GUARDED,
    ),
)
async def list_observations(
        filters: Annotated[ObservationFilterParams, Query()],
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.OBSERVATION_VIEW]))
):
    """List observations with the ids of their media. Every filter is an optional query parameter.

    The observations of the posts where observation.view is in effect; archived ones only with include_deleted
    (requires observation.archive), and only at the posts where observation.archive is in effect. Media files are
    loaded with selectinload: two queries for any number of observations."""
    archived = AccessControlService.archived_scope(auth, filters, Permissions.OBSERVATION_ARCHIVE)
    filtered_post_ids = AccessControlService.resolve_scope(auth, Permissions.OBSERVATION_VIEW, filters.post_id)

    # Build base query and apply filters
    query = select(Observation).options(*OBSERVATION_LOAD_OPTIONS)
    query = ObservationFilterService.apply_filters(query, filters, filtered_post_ids, archived_post_ids=archived,
                                                   unit_condition=observation_units_filter(auth.tree, filters))

    # Execute query
    result = await session.exec(query)
    observations = result.all()

    return await observations_to_response(session, observations)


@router.post(
    ObservationRoutes.CREATE.value,
    response_model=ObservationWithMedia,
    responses=problems(
        ErrorCode.SIGNAL_TYPE_INACTIVE, AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.POST_ACCESS_DENIED,
        AccessErrorCode.POST_WRITE_DENIED, ErrorCode.POST_NOT_FOUND, ErrorCode.POST_ARCHIVED, ErrorCode.SIGNAL_TYPE_NOT_FOUND,
        ErrorCode.TAGS_NOT_FOUND, *GUARDED,
    ),
)
async def create_observation(
        request: Request,
        observation_data: ObservationCreate,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.OBSERVATION_VIEW, Permissions.OBSERVATION_CREATE]))
):
    """
    Create new observation.
    Needs observation.view and observation.create in effect at the post. An archived post: 409 post_archived.
    Note: Field validation is handled by Pydantic model validators.
    """
    current_user = auth.user
    # Access first: an unknown post id and a foreign one answer alike (403); only a caller whose permissions apply
    # at every post (held at the root) learns 404
    AccessControlService.check_post(auth, Permissions.OBSERVATION_VIEW, observation_data.post_id, "observation")
    AccessControlService.check_post(auth, Permissions.OBSERVATION_CREATE, observation_data.post_id, "observation")

    post = (await session.exec(select(Post).where(Post.id == observation_data.post_id))).first()
    if not post:
        raise ApiError(ErrorCode.POST_NOT_FOUND, post_id=observation_data.post_id)
    refuse_archived_post(post)

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
    await session.flush()
    # The units the post sits in now, recorded once with the observation (services/units.py)
    session.add_all(observation_unit_rows(auth.tree, post.group_id, observation.id, observation.created_at))
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


@router.put(
    ObservationRoutes.OBSERVATION_BY_ID.value,
    response_model=ObservationWithMedia,
    responses=problems(
        ErrorCode.SIGNAL_TYPE_INACTIVE, AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.OBSERVATION_NOT_OWNER,
        AccessErrorCode.POST_ACCESS_DENIED, AccessErrorCode.POST_WRITE_DENIED, ErrorCode.OBSERVATION_NOT_FOUND,
        ErrorCode.POST_ARCHIVED, ErrorCode.SIGNAL_TYPE_NOT_FOUND, ErrorCode.TAGS_NOT_FOUND, *GUARDED,
    ),
)
async def update_observation(
        request: Request,
        observation_id: int,
        observation_data: ObservationUpdate,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.OBSERVATION_VIEW, Permissions.OBSERVATION_UPDATE]))
):
    """
    Update existing observation (tags included).
    Needs observation.view and observation.update in effect at its post; owner or observation.manage_any there.
    Archived observations answer 404 until restored (POST /{observation_id}/restore); an archived post: 409 post_archived.
    """
    current_user = auth.user
    # Get observation by ID with tags and bands pre-loaded, locked: concurrent changes run one after another
    query = select(Observation).where(
        Observation.id == observation_id
    ).options(selectinload(Observation.tags), selectinload(Observation.all_frequencies))
    observation = await lock_first(session, query)

    if not observation:
        raise ApiError(ErrorCode.OBSERVATION_NOT_FOUND)

    # Store old values for auditing
    old_values = observation_audit_values(observation)

    # Permission at the observation's post, then ownership
    AccessControlService.check_post(auth, Permissions.OBSERVATION_VIEW, observation.post_id, "observation")
    AccessControlService.check_post(auth, Permissions.OBSERVATION_UPDATE, observation.post_id, "observation")
    AccessControlService.check_observation_ownership(auth, observation)
    await ensure_post_not_archived(session, observation.post_id)

    if observation.is_deleted:
        raise ApiError(ErrorCode.OBSERVATION_NOT_FOUND)

    if observation_data.signal_type_id is not None and observation_data.signal_type_id != observation.signal_type_id:
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
    if "observed_at" in update_data and observation.disappeared_at is not None:
        # the duration of a disappeared observation follows its start unless the request sets it
        duration = disappeared_duration(update_data["observed_at"], observation.disappeared_at, "observed_at")
        update_data.setdefault("duration_seconds", duration)
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


@router.post(
    ObservationRoutes.MARK_DISAPPEARED.value,
    response_model=MessageResponse,
    responses=problems(
        AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.OBSERVATION_NOT_OWNER, AccessErrorCode.POST_ACCESS_DENIED,
        AccessErrorCode.POST_WRITE_DENIED, ErrorCode.OBSERVATION_NOT_FOUND, ErrorCode.POST_ARCHIVED, *GUARDED,
    ),
)
async def mark_observation_disappeared(
        request: Request,
        observation_id: int,
        disappeared_data: ObservationDisappeared,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.OBSERVATION_VIEW, Permissions.OBSERVATION_UPDATE]))
):
    """
    Mark observation as disappeared.
    Needs observation.view and observation.update in effect at its post; owner or observation.manage_any there.
    An archived post: 409 post_archived.
    """
    current_user = auth.user
    observation = await lock_first(session, select(Observation).where(
        Observation.id == observation_id,
        Observation.is_deleted == False
    ))

    if not observation:
        raise ApiError(ErrorCode.OBSERVATION_NOT_FOUND)

    # Store old values for auditing
    old_values = observation.model_dump()

    # Permission at the observation's post, then ownership
    AccessControlService.check_post(auth, Permissions.OBSERVATION_VIEW, observation.post_id, "observation")
    AccessControlService.check_post(auth, Permissions.OBSERVATION_UPDATE, observation.post_id, "observation")
    AccessControlService.check_observation_ownership(auth, observation)
    await ensure_post_not_archived(session, observation.post_id)

    duration = disappeared_duration(observation.observed_at, disappeared_data.disappeared_at, "disappeared_at")

    observation.status = ObservationStatus.DISAPPEARED.value
    observation.disappeared_at = disappeared_data.disappeared_at
    observation.disappeared_by = current_user.id
    observation.updated_at = datetime.now(timezone.utc)
    observation.duration_seconds = duration

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


@router.get(ObservationRoutes.REPORT_TYPES.value, response_model=List[ReportTypeChoice], responses=problems(*GUARDED))
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
    responses={
        **file_response("Excel report", ["application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"],
                            {"Content-Disposition": header('attachment; filename="<report>.xlsx"')}),
        **problems(
            AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION,
            AccessErrorCode.POST_ACCESS_DENIED, AccessErrorCode.POST_WRITE_DENIED, *GUARDED,
        ),
    },
)
async def generate_observation_report(
        params: ObservationReportParams,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.OBSERVATION_VIEW, Permissions.EXPORT_CREATE]))
):
    """
    Generate an Excel (.xlsx) report for observations (observation.view and export.create).

    The report header contains the filter criteria used (in Ukrainian).
    One row per frequency band (rc_freq_part: rc bands, video_freq_part: video bands, all_fields: every band);
    the observation columns repeat for each of its bands. Bands of archived observations and archived bands
    appear only with include_deleted (requires observation.archive), and only for the posts where
    observation.archive is in effect. Only the posts where both observation.view and export.create are in effect
    are included; a post_ids entry outside them answers 403.
    corps_ids / brigade_ids narrow the report to observations of these units (placement: recorded, where the post
    was when the observation was created, or current, where it is now); access always follows the current tree.
    all_fields columns: user, post, corps, brigade (as the placement says; an archived unit is marked), signal type,
    band kind, emitter group, frequency min/max/center, bandwidth,
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
    archived = AccessControlService.archived_scope(auth, params, Permissions.OBSERVATION_ARCHIVE)
    for post_id in params.post_ids or []:
        AccessControlService.check_post(auth, Permissions.OBSERVATION_VIEW, post_id)
        AccessControlService.check_post(auth, Permissions.EXPORT_CREATE, post_id)
    scope = AccessControlService.scope_for_all(auth, Permissions.OBSERVATION_VIEW, Permissions.EXPORT_CREATE)
    asked_units = {kind: getattr(params, f"{kind.value}_ids") for kind in UNIT_KINDS}
    query = ObservationReportFilter.build_query(
        params, scope, archived_post_ids=archived,
        unit_condition=observation_units_any(auth.tree, asked_units, params.placement),
    )

    result = await session.exec(query)
    frequencies = list(result.all())
    units = await observation_unit_labels(
        session, auth.tree, {band.observation.id: band.observation for band in frequencies}.values(), params.placement)
    # Names only of the units the caller sees or that the report's own columns show; others as their number
    named = {label.id for found in units.values() for label in found.values()} | set(visible_unit_ids(auth, asked_units))
    unit_filters = await unit_labels(session, (
        unit_id for ids in asked_units.values() for unit_id in ids or () if unit_id in named))

    tag_names = None
    if params.tag_ids:
        tag_rows = await session.exec(select(Tag.id, Tag.name).where(Tag.id.in_(params.tag_ids)))
        tag_names = {tag_id: name for tag_id, name in tag_rows.all()}
    excel_bytes = ObservationReportService.generate_excel(frequencies, params, tag_names=tag_names, units=units,
                                                          unit_filters=unit_filters)

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
    """Shared body of archive / restore: observation.view and observation.archive in effect at the post, ownership
    (observation.manage_any in effect there overrides), flag flip, audit row. In an archived post only grants
    inherited from a group or the root apply (a grant on the post itself stops applying: 403), and only callers with
    post.archive in effect at the post archive and restore; everyone else gets 409 post_archived."""
    current_user = auth.user
    observation = await lock_first(session, select(Observation).where(Observation.id == observation_id))
    if not observation:
        raise ApiError(ErrorCode.OBSERVATION_NOT_FOUND)

    AccessControlService.check_post(auth, Permissions.OBSERVATION_VIEW, observation.post_id, "observation")
    AccessControlService.check_post(auth, Permissions.OBSERVATION_ARCHIVE, observation.post_id, "observation")
    AccessControlService.check_observation_ownership(auth, observation)
    if not auth.has_at_post(Permissions.POST_ARCHIVE, observation.post_id):
        await ensure_post_not_archived(session, observation.post_id)

    # The state after the scope and ownership checks: an outsider gets the same 403 whatever the row looks like
    if archived and observation.is_deleted:
        raise ApiError(ErrorCode.OBSERVATION_NOT_FOUND)
    if not archived and not observation.is_deleted:
        raise ApiError(ErrorCode.OBSERVATION_NOT_ARCHIVED)

    if not archived:
        signal_type = (await session.exec(
            select(SignalType).where(SignalType.id == observation.signal_type_id)
        )).first()
        if signal_type is not None and signal_type.is_deleted:
            raise ApiError(ErrorCode.OBSERVATION_SIGNAL_TYPE_ARCHIVED)

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


@router.post(
    ObservationRoutes.OBSERVATION_ARCHIVE.value,
    response_model=ObservationWithMedia,
    responses=problems(
        AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.OBSERVATION_NOT_OWNER, AccessErrorCode.POST_ACCESS_DENIED,
        AccessErrorCode.POST_WRITE_DENIED, ErrorCode.OBSERVATION_NOT_FOUND, ErrorCode.OBSERVATION_NOT_ARCHIVED,
        ErrorCode.POST_ARCHIVED, *GUARDED,
    ),
)
async def archive_observation(
        request: Request,
        observation_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.OBSERVATION_VIEW, Permissions.OBSERVATION_ARCHIVE]))
):
    """Archive an observation (is_deleted=true). observation.view and observation.archive in effect at its post;
    owner or observation.manage_any in effect there.

    Nothing is removed: media files stay attached, the row is hidden from lists unless include_deleted=true.
    Undo with POST /{observation_id}/restore. Already archived or unknown: 404. In an archived post it also needs
    post.archive in effect at the post (otherwise 409 post_archived); a grant on the archived post itself no longer
    applies (403).
    """
    return await _set_observation_archived(request, session, auth, observation_id, archived=True)


@router.post(
    ObservationRoutes.OBSERVATION_RESTORE.value,
    response_model=ObservationWithMedia,
    responses=problems(
        AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.OBSERVATION_NOT_OWNER, AccessErrorCode.POST_ACCESS_DENIED,
        AccessErrorCode.POST_WRITE_DENIED, ErrorCode.OBSERVATION_NOT_FOUND, ErrorCode.OBSERVATION_NOT_ARCHIVED, ErrorCode.OBSERVATION_SIGNAL_TYPE_ARCHIVED,
        ErrorCode.POST_ARCHIVED, *GUARDED,
    ),
)
async def restore_observation(
        request: Request,
        observation_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.OBSERVATION_VIEW, Permissions.OBSERVATION_ARCHIVE]))
):
    """Restore an archived observation (is_deleted=false). Same rules as archive (an archived post: post.archive
    in effect at it). Not archived: 409."""
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


@router.get(
    ObservationRoutes.OBSERVATION_FREQUENCIES.value,
    response_model=List[ObservationFrequencyResponse],
    responses=problems(
        AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.PERMISSION_REQUIRED_FOR_ACTION,
        AccessErrorCode.POST_ACCESS_DENIED, ErrorCode.OBSERVATION_NOT_FOUND, *GUARDED,
    ),
)
async def list_observation_frequencies(
        observation_id: int,
        params: ObservationFrequencyListParams = Depends(),
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.OBSERVATION_VIEW]))
):
    """Bands of one observation. Archived bands and bands of an archived observation need include_deleted=true
    (requires observation.archive) and observation.archive in effect at the observation's post."""
    archived = AccessControlService.archived_scope(auth, params, Permissions.OBSERVATION_ARCHIVE)
    observation = (await session.exec(select(Observation).where(Observation.id == observation_id))).first()
    if not observation:
        raise ApiError(ErrorCode.OBSERVATION_NOT_FOUND)
    AccessControlService.check_post(auth, Permissions.OBSERVATION_VIEW, observation.post_id, "observation")
    include_deleted = params.include_deleted and (archived is None or observation.post_id in archived)
    if observation.is_deleted and not include_deleted:
        raise ApiError(ErrorCode.OBSERVATION_NOT_FOUND)

    result = await session.exec(ObservationFrequencyFilter.of_observation(observation_id, include_deleted))
    return [ObservationFrequencyResponse.model_validate(f) for f in result.all()]


@router.post(
    ObservationRoutes.OBSERVATION_FREQUENCIES.value,
    response_model=ObservationFrequencyResponse,
    responses=problems(
        AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.OBSERVATION_NOT_OWNER, AccessErrorCode.POST_ACCESS_DENIED,
        AccessErrorCode.POST_WRITE_DENIED, ErrorCode.OBSERVATION_NOT_FOUND, ErrorCode.POST_ARCHIVED, *GUARDED,
    ),
)
async def add_observation_frequency(
        request: Request,
        observation_id: int,
        frequency_data: ObservationFrequencyCreate,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.OBSERVATION_VIEW, Permissions.OBSERVATION_UPDATE]))
):
    """Add a band to an observation. observation.view and observation.update in effect at its post; owner or
    observation.manage_any in effect there.
    Archived or unknown observation: 404. An archived post: 409 post_archived (every band change)."""
    observation = await get_observation_for_write(session, auth, observation_id, lock=True)

    frequency = frequency_rows([frequency_data])[0]
    observation.all_frequencies.append(frequency)
    observation.updated_at = datetime.now(timezone.utc)
    session.add(observation)
    await session.commit()

    await _audit_frequency(request, session, auth, "create", frequency)
    await session.commit()
    return ObservationFrequencyResponse.model_validate(frequency)


@router.put(
    ObservationRoutes.OBSERVATION_FREQUENCY_BY_ID.value,
    response_model=ObservationFrequencyResponse,
    responses=problems(
        AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.OBSERVATION_NOT_OWNER, AccessErrorCode.POST_ACCESS_DENIED,
        AccessErrorCode.POST_WRITE_DENIED, ErrorCode.FREQUENCY_NOT_FOUND, ErrorCode.OBSERVATION_NOT_FOUND,
        ErrorCode.POST_ARCHIVED, *GUARDED,
    ),
)
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
    frequency_center = changes.get("frequency_center", frequency.frequency_center)
    if {"frequency_min", "frequency_max", "frequency_center"} & changes.keys() and not frequency_min <= frequency_center <= frequency_max:
        raise field_problem(FieldErrorCode.FREQUENCY_CENTER_OUT_OF_RANGE, ("body", "frequency_center"))

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
    """Shared body of band archive / restore: observation.view and observation.archive in effect at the post"""
    observation = await get_observation_for_write(
        session, auth, observation_id, lock=True, permission=Permissions.OBSERVATION_ARCHIVE,
    )
    frequency = get_frequency_or_404(observation, frequency_id)
    if archived and frequency.is_deleted:
        raise ApiError(ErrorCode.FREQUENCY_NOT_FOUND)
    if not archived and not frequency.is_deleted:
        raise ApiError(ErrorCode.FREQUENCY_NOT_ARCHIVED)
    if archived:
        await ensure_other_active_frequency(session, observation.id, frequency.id)

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


@router.post(
    ObservationRoutes.OBSERVATION_FREQUENCY_ARCHIVE.value,
    response_model=ObservationFrequencyResponse,
    responses=problems(
        AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.OBSERVATION_NOT_OWNER, AccessErrorCode.POST_ACCESS_DENIED,
        AccessErrorCode.POST_WRITE_DENIED, ErrorCode.FREQUENCY_NOT_FOUND, ErrorCode.OBSERVATION_NOT_FOUND,
        ErrorCode.FREQUENCY_NOT_ARCHIVED, ErrorCode.LAST_FREQUENCY_NOT_ARCHIVABLE, ErrorCode.POST_ARCHIVED, *GUARDED,
    ),
)
async def archive_observation_frequency(
        request: Request,
        observation_id: int,
        frequency_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.OBSERVATION_VIEW, Permissions.OBSERVATION_ARCHIVE]))
):
    """Archive a band (is_deleted=true): it leaves the observation response, lists, filters and reports.
    Owner or observation.manage_any, observation.view and observation.archive in effect at the post. Already
    archived, unknown, or archived observation: 404. The last active band of an observation: 409."""
    return await _set_frequency_archived(request, session, auth, observation_id, frequency_id, archived=True)


@router.post(
    ObservationRoutes.OBSERVATION_FREQUENCY_RESTORE.value,
    response_model=ObservationFrequencyResponse,
    responses=problems(
        AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.OBSERVATION_NOT_OWNER, AccessErrorCode.POST_ACCESS_DENIED,
        AccessErrorCode.POST_WRITE_DENIED, ErrorCode.FREQUENCY_NOT_FOUND, ErrorCode.OBSERVATION_NOT_FOUND,
        ErrorCode.FREQUENCY_NOT_ARCHIVED, ErrorCode.LAST_FREQUENCY_NOT_ARCHIVABLE, ErrorCode.POST_ARCHIVED, *GUARDED,
    ),
)
async def restore_observation_frequency(
        request: Request,
        observation_id: int,
        frequency_id: int,
        session: AsyncSession = Depends(get_session),
        auth: AuthContext = Depends(require(all=[Permissions.OBSERVATION_VIEW, Permissions.OBSERVATION_ARCHIVE]))
):
    """Restore an archived band. Same rules as archive. Not archived: 409."""
    return await _set_frequency_archived(request, session, auth, observation_id, frequency_id, archived=False)
