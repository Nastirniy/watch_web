"""
Signal monitoring API routes for observations, signal types, and analysis.
Handles signal detection data and classification systems.
"""

from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from loguru import logger

from backend.core.exceptions import (
    AuthorizationException,
    ResourceNotFoundException,
    ValidationException,
)
from backend.core.permissions import SystemPermissions
from backend.database.crud.audit import audit_log as audit_crud
from backend.database.crud.signal import (
    observation as observation_crud,
    signal_type as signal_type_crud,
)
from backend.routes.dependencies import DatabaseDep, CurrentUser, OptionalUser, require_permissions
from backend.database.models import AuditAction, AuditSeverity, ResourceType
from backend.api.schemas import (
    BulkOperationResponse,
    MessageResponse,
    PaginatedResponse,
    create_paginated_response,
)
from backend.api.schemas import (
    BulkObservationUpdate,
    Observation,
    ObservationCreate,
    ObservationSearchFilters,
    ObservationStatistics,
    ObservationSummary,
    ObservationUpdate,
    SignalType,
    SignalTypeCreate,
    SignalTypeUpdate,
)

router = APIRouter()


# Signal Type Management
@router.post("/types", response_model=SignalType, status_code=status.HTTP_201_CREATED)
async def create_signal_type(
        signal_type_data: SignalTypeCreate,
        db: DatabaseDep,
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.SIGNAL_CREATE)),
) -> SignalType:
    """
    Create a new signal type.

    Args:
        signal_type_data: Signal type creation data
        db: Database session
        current_user: Current authenticated user

    Returns:
        Created signal type
    """
    try:
        # Check if code already exists
        existing = await signal_type_crud.get_by_code(db, signal_type_data.code)
        if existing:
            raise ValidationException(
                f"Signal type with code '{signal_type_data.code}' already exists",
                field="code"
            )

        # Create signal type
        signal_type = await signal_type_crud.create(db, signal_type_data.model_dump())

        # Log creation
        await audit_crud.create_log(
            db=db,
            action=AuditAction.CREATE,
            resource_type=ResourceType.SIGNALS,
            user_id=current_user.id,
            resource_id=signal_type.id,
            resource_name=signal_type.name,
            new_values=signal_type_data.model_dump(),
            success=True,
            severity=AuditSeverity.LOW,
        )

        logger.info(f"Signal type {signal_type.name} created by {current_user.username}")
        return signal_type

    except ValidationException:
        raise
    except Exception as e:
        logger.error(f"Error creating signal type: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create signal type"
        )


@router.get("/types", response_model=List[SignalType])
async def list_signal_types(
        db: DatabaseDep,
        current_user: OptionalUser,
        active_only: bool = Query(True, description="Return only active signal types"),
        category_id: Optional[int] = Query(None, description="Filter by category ID"),
) -> List[SignalType]:
    """
    List signal types.

    Args:
        active_only: Return only active signal types
        category_id: Filter by category ID
        db: Database session
        current_user: Optional current user

    Returns:
        List of signal types
    """
    try:
        if active_only:
            signal_types = await signal_type_crud.get_active_types(db)
        elif category_id:
            signal_types = await signal_type_crud.get_by_category(db, category_id)
        else:
            signal_types = await signal_type_crud.get_multi(db, limit=1000)

        return signal_types

    except Exception as e:
        logger.error(f"Error listing signal types: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve signal types"
        )


@router.get("/types/{signal_type_id}", response_model=SignalType)
async def get_signal_type(
        signal_type_id: int,
        db: DatabaseDep,
        current_user: OptionalUser,
) -> SignalType:
    """
    Get signal type by ID.

    Args:
        signal_type_id: Signal type ID
        db: Database session
        current_user: Optional current user

    Returns:
        Signal type details
    """
    signal_type = await signal_type_crud.get(db, signal_type_id, load_relationships=["category"])
    if not signal_type:
        raise ResourceNotFoundException("Signal type", signal_type_id)

    return signal_type


@router.put("/types/{signal_type_id}", response_model=SignalType)
async def update_signal_type(
        signal_type_id: int,
        update_data: SignalTypeUpdate,
        db: DatabaseDep,
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.SIGNAL_UPDATE)),
) -> SignalType:
    """
    Update signal type.

    Args:
        signal_type_id: Signal type ID
        update_data: Update data
        db: Database session
        current_user: Current authenticated user

    Returns:
        Updated signal type
    """
    # Get existing signal type
    signal_type = await signal_type_crud.get(db, signal_type_id)
    if not signal_type:
        raise ResourceNotFoundException("Signal type", signal_type_id)

    try:
        # Store old values for audit
        old_values = {
            "name": signal_type.name,
            "description": signal_type.description,
            "is_active": signal_type.is_active,
        }

        # Update signal type
        updated_signal_type = await signal_type_crud.update(
            db, signal_type, update_data.model_dump(exclude_unset=True)
        )

        # Log update
        await audit_crud.create_log(
            db=db,
            action=AuditAction.UPDATE,
            resource_type=ResourceType.SIGNALS,
            user_id=current_user.id,
            resource_id=signal_type_id,
            resource_name=updated_signal_type.name,
            old_values=old_values,
            new_values=update_data.model_dump(exclude_unset=True),
            success=True,
            severity=AuditSeverity.LOW,
        )

        logger.info(f"Signal type {updated_signal_type.name} updated by {current_user.username}")
        return updated_signal_type

    except Exception as e:
        logger.error(f"Error updating signal type {signal_type_id}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update signal type"
        )


# Observation Management
@router.post("/observations", response_model=Observation, status_code=status.HTTP_201_CREATED)
async def create_observation(
        observation_data: ObservationCreate,
        db: DatabaseDep,
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.OBSERVATION_CREATE)),
) -> Observation:
    """
    Create a new observation.

    Args:
        observation_data: Observation creation data
        db: Database session
        current_user: Current authenticated user

    Returns:
        Created observation
    """
    try:
        # Add user ID to observation data
        obs_dict = observation_data.model_dump()
        obs_dict["user_id"] = current_user.id

        # Create observation
        observation = await observation_crud.create(db, obs_dict)

        # Update user's last record created timestamp
        current_user.last_record_created_at = observation.created_at
        await db.commit()

        # Log creation
        await audit_crud.create_log(
            db=db,
            action=AuditAction.CREATE,
            resource_type=ResourceType.OBSERVATIONS,
            user_id=current_user.id,
            resource_id=observation.id,
            resource_name=f"Observation #{observation.id}",
            new_values={
                "post_id": observation.post_id,
                "signal_type_id": observation.signal_type_id,
                "frequency_range": f"{observation.frequency_min}-{observation.frequency_max} MHz",
            },
            success=True,
            severity=AuditSeverity.LOW,
        )

        logger.info(f"Observation {observation.id} created by {current_user.username}")

        # Return observation with relationships loaded
        return await observation_crud.get_with_relations(db, observation.id)

    except Exception as e:
        logger.error(f"Error creating observation: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create observation"
        )


@router.get("/observations", response_model=PaginatedResponse[ObservationSummary])
async def list_observations(
        db: DatabaseDep,
        filters: ObservationSearchFilters = Depends(),
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.OBSERVATION_READ)),
) -> PaginatedResponse[ObservationSummary]:
    """
    List observations with filtering and pagination.

    Args:
        filters: Search filters
        db: Database session
        current_user: Current authenticated user

    Returns:
        Paginated list of observations
    """
    try:
        # Build search parameters
        search_params = {}

        if filters.post_ids:
            search_params["post_ids"] = filters.post_ids
        if filters.user_ids:
            search_params["user_ids"] = filters.user_ids
        if filters.signal_type_ids:
            search_params["signal_type_ids"] = filters.signal_type_ids
        if filters.statuses:
            search_params["statuses"] = filters.statuses
        if filters.confidence_levels:
            search_params["confidence_levels"] = filters.confidence_levels
        if filters.frequency_min:
            search_params["frequency_min"] = filters.frequency_min
        if filters.frequency_max:
            search_params["frequency_max"] = filters.frequency_max
        if filters.signal_strength_min:
            search_params["signal_strength_min"] = filters.signal_strength_min
        if filters.signal_strength_max:
            search_params["signal_strength_max"] = filters.signal_strength_max
        if filters.date_from:
            search_params["date_from"] = filters.date_from
        if filters.date_to:
            search_params["date_to"] = filters.date_to

        search_params["include_deleted"] = filters.include_deleted
        search_params["order_by"] = filters.sort_by or "observed_at"
        search_params["order_direction"] = filters.sort_order

        # Search observations
        observations, total = await observation_crud.search_observations(
            db=db,
            search_params=search_params,
            skip=(filters.page - 1) * filters.per_page,
            limit=filters.per_page,
        )

        # Convert to summary format
        observation_summaries = []
        for obs in observations:
            observation_summaries.append(ObservationSummary(
                id=obs.id,
                post_id=obs.post_id,
                signal_type_id=obs.signal_type_id,
                frequency_min=obs.frequency_min,
                frequency_max=obs.frequency_max,
                frequency_center=obs.frequency_center,
                observed_at=obs.observed_at,
                signal_strength=obs.signal_strength,
                status=obs.status,
                confidence_level=obs.confidence_level,
                user_id=obs.user_id,
                username=obs.user.username if obs.user else None,
                post_name=obs.post.name if obs.post else None,
                signal_type_name=obs.signal_type.name if obs.signal_type else None,
            ))

        return create_paginated_response(
            items=observation_summaries,
            total=total,
            page=filters.page,
            per_page=filters.per_page,
        )

    except Exception as e:
        logger.error(f"Error listing observations: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve observations"
        )


@router.get("/observations/{observation_id}", response_model=Observation)
async def get_observation(
        observation_id: int,
        db: DatabaseDep,
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.OBSERVATION_READ)),
) -> Observation:
    """
    Get observation by ID.

    Args:
        observation_id: Observation ID
        db: Database session
        current_user: Current authenticated user

    Returns:
        Observation details
    """
    observation = await observation_crud.get_with_relations(db, observation_id)
    if not observation:
        raise ResourceNotFoundException("Observation", observation_id)

    return observation


@router.put("/observations/{observation_id}", response_model=Observation)
async def update_observation(
        observation_id: int,
        update_data: ObservationUpdate,
        db: DatabaseDep,
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.OBSERVATION_UPDATE)),
) -> Observation:
    """
    Update observation.

    Args:
        observation_id: Observation ID
        update_data: Update data
        db: Database session
        current_user: Current authenticated user

    Returns:
        Updated observation
    """
    # Get existing observation
    observation = await observation_crud.get(db, observation_id)
    if not observation:
        raise ResourceNotFoundException("Observation", observation_id)

    # Check if user can update this observation
    if (observation.user_id != current_user.id and
            not current_user.is_superuser and
            not await current_user.has_permission(SystemPermissions.OBSERVATION_MANAGE)):
        raise AuthorizationException("Can only update your own observations")

    try:
        # Store old values for audit
        old_values = {
            "status": observation.status.value,
            "confidence_level": observation.confidence_level.value,
            "comment": observation.comment,
        }

        # Update observation
        updated_observation = await observation_crud.update(
            db, observation, update_data.model_dump(exclude_unset=True)
        )

        # Log update
        await audit_crud.create_log(
            db=db,
            action=AuditAction.UPDATE,
            resource_type=ResourceType.OBSERVATIONS,
            user_id=current_user.id,
            resource_id=observation_id,
            resource_name=f"Observation #{observation_id}",
            old_values=old_values,
            new_values=update_data.model_dump(exclude_unset=True),
            success=True,
            severity=AuditSeverity.LOW,
        )

        logger.info(f"Observation {observation_id} updated by {current_user.username}")

        # Return updated observation with relationships
        return await observation_crud.get_with_relations(db, observation_id)

    except AuthorizationException:
        raise
    except Exception as e:
        logger.error(f"Error updating observation {observation_id}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update observation"
        )


@router.delete("/observations/{observation_id}", response_model=MessageResponse)
async def delete_observation(
        observation_id: int,
        db: DatabaseDep,
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.OBSERVATION_DELETE)),
) -> MessageResponse:
    """
    Delete observation (soft delete).

    Args:
        observation_id: Observation ID
        db: Database session
        current_user: Current authenticated user

    Returns:
        Deletion confirmation
    """
    # Get observation
    observation = await observation_crud.get(db, observation_id)
    if not observation:
        raise ResourceNotFoundException("Observation", observation_id)

    # Check if user can delete this observation
    if (observation.user_id != current_user.id and
            not current_user.is_superuser and
            not await current_user.has_permission(SystemPermissions.OBSERVATION_MANAGE)):
        raise AuthorizationException("Can only delete your own observations")

    try:
        # Soft delete observation
        await observation_crud.soft_delete(db, observation_id)

        # Log deletion
        await audit_crud.create_log(
            db=db,
            action=AuditAction.SOFT_DELETE,
            resource_type=ResourceType.OBSERVATIONS,
            user_id=current_user.id,
            resource_id=observation_id,
            resource_name=f"Observation #{observation_id}",
            old_values={"status": observation.status.value},
            success=True,
            severity=AuditSeverity.MEDIUM,
        )

        logger.info(f"Observation {observation_id} deleted by {current_user.username}")

        return MessageResponse(message="Observation deleted successfully")

    except AuthorizationException:
        raise
    except Exception as e:
        logger.error(f"Error deleting observation {observation_id}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete observation"
        )


# Bulk Operations
@router.post("/observations/bulk-update", response_model=BulkOperationResponse)
async def bulk_update_observations(
        bulk_update: BulkObservationUpdate,
        db: DatabaseDep,
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.OBSERVATION_UPDATE)),
) -> BulkOperationResponse:
    """
    Bulk update observations.

    Args:
        bulk_update: Bulk update data
        db: Database session
        current_user: Current authenticated user

    Returns:
        Bulk operation result
    """
    try:
        processed = len(bulk_update.observation_ids)
        succeeded = 0
        errors = []

        for obs_id in bulk_update.observation_ids:
            try:
                # Check if observation exists and user has permission
                observation = await observation_crud.get(db, obs_id)
                if not observation:
                    errors.append({
                        "observation_id": obs_id,
                        "error": "Observation not found"
                    })
                    continue

                # Check permissions
                if (observation.user_id != current_user.id and
                        not current_user.is_superuser and
                        not await current_user.has_permission(SystemPermissions.OBSERVATION_MANAGE)):
                    errors.append({
                        "observation_id": obs_id,
                        "error": "Permission denied"
                    })
                    continue

                # Apply updates
                update_data = {}
                if bulk_update.status:
                    update_data["status"] = bulk_update.status
                if bulk_update.signal_type_id:
                    update_data["signal_type_id"] = bulk_update.signal_type_id
                if bulk_update.confidence_level:
                    update_data["confidence_level"] = bulk_update.confidence_level
                if bulk_update.comment_append:
                    current_comment = observation.comment or ""
                    update_data["comment"] = f"{current_comment}\n{bulk_update.comment_append}".strip()

                if update_data:
                    await observation_crud.update(db, observation, update_data)
                    succeeded += 1

            except Exception as e:
                errors.append({
                    "observation_id": obs_id,
                    "error": str(e)
                })

        # Log bulk operation
        await audit_crud.create_log(
            db=db,
            action=AuditAction.UPDATE,
            resource_type=ResourceType.OBSERVATIONS,
            user_id=current_user.id,
            resource_name="bulk_update",
            new_values={
                "observation_ids": bulk_update.observation_ids,
                "updates": bulk_update.model_dump(exclude={"observation_ids"}),
                "processed": processed,
                "succeeded": succeeded,
            },
            success=len(errors) == 0,
            severity=AuditSeverity.MEDIUM,
        )

        return BulkOperationResponse(
            message="Bulk update completed",
            processed=processed,
            succeeded=succeeded,
            failed=processed - succeeded,
            errors=errors,
        )

    except Exception as e:
        logger.error(f"Error in bulk update: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Bulk update failed"
        )


# Statistics and Analysis
@router.get("/observations/statistics", response_model=ObservationStatistics)
async def get_observation_statistics(
        db: DatabaseDep,
        start_date: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
        end_date: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
        post_id: Optional[int] = Query(None, description="Filter by post ID"),
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.OBSERVATION_READ)),
) -> ObservationStatistics:
    """
    Get observation statistics.

    Args:
        start_date: Optional start date filter
        end_date: Optional end date filter
        post_id: Optional post filter
        db: Database session
        current_user: Current authenticated user

    Returns:
        Observation statistics
    """
    try:
        from datetime import datetime

        # Parse dates
        start_dt = None
        end_dt = None
        if start_date:
            start_dt = datetime.strptime(start_date, "%Y-%m-%d").date()
        if end_date:
            end_dt = datetime.strptime(end_date, "%Y-%m-%d").date()

        # Get statistics
        stats = await observation_crud.get_statistics(db, start_dt, end_dt, post_id)

        return ObservationStatistics(
            total_observations=stats["total"],
            active_observations=stats["active"],
            by_status=stats,
            by_confidence={},  # Would need additional implementation
            by_post={},  # Would need additional implementation
            by_signal_type={},  # Would need additional implementation
            by_user={},  # Would need additional implementation
            frequency_range={},  # Would need additional implementation
            time_range={},  # Would need additional implementation
        )

    except Exception as e:
        logger.error(f"Error getting observation statistics: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve observation statistics"
        )


@router.post("/observations/{observation_id}/mark-disappeared", response_model=MessageResponse)
async def mark_observation_disappeared(
        db: DatabaseDep,
        observation_id: int,
        comment: Optional[str] = None,
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.OBSERVATION_UPDATE)),
) -> MessageResponse:
    """
    Mark observation as disappeared.

    Args:
        observation_id: Observation ID
        comment: Optional comment about disappearance
        db: Database session
        current_user: Current authenticated user

    Returns:
        Operation confirmation
    """
    try:
        observation = await observation_crud.mark_disappeared(
            db, observation_id, current_user.id, comment
        )

        if not observation:
            raise ResourceNotFoundException("Observation", observation_id)

        logger.info(f"Observation {observation_id} marked as disappeared by {current_user.username}")

        return MessageResponse(message="Observation marked as disappeared")

    except ResourceNotFoundException:
        raise
    except Exception as e:
        logger.error(f"Error marking observation as disappeared: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to mark observation as disappeared"
        )
