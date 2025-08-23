"""
Export API routes for data export and reporting functionality.
Handles export job creation, monitoring, and file download.
"""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import FileResponse
from loguru import logger

from backend.core.exceptions import (
    ResourceNotFoundException,
    ValidationException,
)
from backend.core.permissions import SystemPermissions
from backend.database.crud.audit import audit_log as audit_crud
from backend.database.crud.export import export_job as export_crud
from backend.routes.dependencies import DatabaseDep, CurrentUser, require_permissions
from backend.database.models import AuditAction, AuditSeverity, ResourceType
from backend.database.models import JobStatus, JobType
from backend.api.schemas import (
    MessageResponse,
    PaginatedResponse,
    create_paginated_response,
)
from backend.api.schemas import (
    BulkExportRequest,
    BulkExportResponse,
    DataExportPolicy,
    ExportJob,
    ExportJobAction,
    ExportJobCreate,
    ExportJobSummary,
    ExportJobUpdate,
    ExportProgress,
    ExportSearchFilters,
    ExportStatistics,
    QueueStatus,
)

router = APIRouter()


@router.post("", response_model=ExportJob, status_code=status.HTTP_201_CREATED)
async def create_export_job(
        job_data: ExportJobCreate,
        db: DatabaseDep,
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.EXPORT_CREATE)),
) -> ExportJob:
    """
    Create a new export job.

    Args:
        job_data: Export job creation data
        db: Database session
        current_user: Current authenticated user

    Returns:
        Created export job
    """
    try:
        # Validate date range
        if job_data.date_from and job_data.date_to and job_data.date_from > job_data.date_to:
            raise ValidationException("Start date must be before end date")

        # Create job configuration
        job_config = job_data.model_dump(exclude={"job_type", "priority"})

        # Create export job
        job = await export_crud.create_export_job(
            db=db,
            user_id=current_user.id,
            job_type=job_data.job_type,
            job_config=job_config,
            priority=job_data.priority,
        )

        # Log job creation
        await audit_crud.create_log(
            db=db,
            action=AuditAction.EXPORT_CREATE,
            resource_type=ResourceType.EXPORTS,
            user_id=current_user.id,
            resource_id=job.id,
            resource_name=f"Export {job.job_type.value}",
            new_values={
                "job_type": job.job_type.value,
                "priority": job.priority,
                "date_range": f"{job_data.date_from} to {job_data.date_to}" if job_data.date_from else None,
            },
            success=True,
            severity=AuditSeverity.LOW,
        )

        logger.info(f"Export job {job.id} created by {current_user.username}")
        return job

    except ValidationException:
        raise
    except Exception as e:
        logger.error(f"Error creating export job: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create export job"
        )


@router.get("", response_model=PaginatedResponse[ExportJobSummary])
async def list_export_jobs(
        db: DatabaseDep,
        filters: ExportSearchFilters = Depends(),
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.EXPORT_READ)),
) -> PaginatedResponse[ExportJobSummary]:
    """
    List export jobs with filtering and pagination.

    Args:
        filters: Search filters
        db: Database session
        current_user: Current authenticated user

    Returns:
        Paginated list of export jobs
    """
    try:
        # Build filter dictionary
        filter_dict = {}

        # Filter by user (non-superusers see only their jobs)
        if not current_user.is_superuser:
            filter_dict["user_id"] = current_user.id
        elif filters.user_ids:
            filter_dict["user_id"] = {"in": filters.user_ids}

        if filters.job_types:
            filter_dict["job_type"] = {"in": [jt.value for jt in filters.job_types]}
        if filters.statuses:
            filter_dict["status"] = {"in": [s.value for s in filters.statuses]}
        if filters.priority_min is not None:
            filter_dict["priority"] = {"min": filters.priority_min}
        if filters.priority_max is not None:
            if "priority" not in filter_dict:
                filter_dict["priority"] = {}
            filter_dict["priority"]["max"] = filters.priority_max
        if filters.created_after:
            filter_dict["created_at"] = {"min": filters.created_after}
        if filters.created_before:
            if "created_at" not in filter_dict:
                filter_dict["created_at"] = {}
            filter_dict["created_at"]["max"] = filters.created_before

        # Get jobs
        jobs = await export_crud.get_multi(
            db=db,
            skip=(filters.page - 1) * filters.per_page,
            limit=filters.per_page,
            filters=filter_dict,
            order_by=f"{'-' if filters.sort_order == 'desc' else ''}{filters.sort_by or 'created_at'}",
        )

        # Get total count
        total = await export_crud.count(db, filter_dict)

        # Convert to summary format
        job_summaries = []
        for job in jobs:
            # Get creator username (would need additional query)
            creator_username = current_user.username if job.user_id == current_user.id else "Unknown"

            job_summaries.append(ExportJobSummary(
                id=job.id,
                job_type=job.job_type,
                name=job.name,
                status=job.status,
                progress_percentage=job.progress_percentage,
                created_at=job.created_at,
                started_at=job.started_at,
                completed_at=job.completed_at,
                output_size=job.output_size,
                creator_username=creator_username,
            ))

        return create_paginated_response(
            items=job_summaries,
            total=total,
            page=filters.page,
            per_page=filters.per_page,
        )

    except Exception as e:
        logger.error(f"Error listing export jobs: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve export jobs"
        )


@router.get("/{job_id}", response_model=ExportJob)
async def get_export_job(
        job_id: int,
        db: DatabaseDep,
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.EXPORT_READ)),
) -> ExportJob:
    """
    Get export job by ID.

    Args:
        job_id: Export job ID
        db: Database session
        current_user: Current authenticated user

    Returns:
        Export job details
    """
    job = await export_crud.get(db, job_id)
    if not job:
        raise ResourceNotFoundException("Export job", job_id)

    # Check ownership (non-superusers can only see their own jobs)
    if not current_user.is_superuser and job.user_id != current_user.id:
        raise ResourceNotFoundException("Export job", job_id)

    return job


@router.put("/{job_id}", response_model=ExportJob)
async def update_export_job(
        job_id: int,
        update_data: ExportJobUpdate,
        db: DatabaseDep,
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.EXPORT_UPDATE)),
) -> ExportJob:
    """
    Update export job (limited fields).

    Args:
        job_id: Export job ID
        update_data: Update data
        db: Database session
        current_user: Current authenticated user

    Returns:
        Updated export job
    """
    # Get existing job
    job = await export_crud.get(db, job_id)
    if not job:
        raise ResourceNotFoundException("Export job", job_id)

    # Check ownership
    if not current_user.is_superuser and job.user_id != current_user.id:
        raise ResourceNotFoundException("Export job", job_id)

    # Only allow updates for pending jobs
    if job.status != JobStatus.PENDING:
        raise ValidationException("Can only update pending jobs")

    try:
        # Update job
        updated_job = await export_crud.update(
            db, job, update_data.model_dump(exclude_unset=True)
        )

        logger.info(f"Export job {job_id} updated by {current_user.username}")
        return updated_job

    except Exception as e:
        logger.error(f"Error updating export job {job_id}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update export job"
        )


@router.delete("/{job_id}", response_model=MessageResponse)
async def delete_export_job(
        job_id: int,
        db: DatabaseDep,
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.EXPORT_DELETE)),
) -> MessageResponse:
    """
    Delete export job.

    Args:
        job_id: Export job ID
        db: Database session
        current_user: Current authenticated user

    Returns:
        Deletion confirmation
    """
    # Get job
    job = await export_crud.get(db, job_id)
    if not job:
        raise ResourceNotFoundException("Export job", job_id)

    # Check ownership
    if not current_user.is_superuser and job.user_id != current_user.id:
        raise ResourceNotFoundException("Export job", job_id)

    try:
        # Delete output file if it exists
        import os
        if job.output_path and os.path.exists(job.output_path):
            os.remove(job.output_path)

        # Delete job
        await export_crud.delete(db, job_id)

        # Log deletion
        await audit_crud.create_log(
            db=db,
            action=AuditAction.DELETE,
            resource_type=ResourceType.EXPORTS,
            user_id=current_user.id,
            resource_id=job_id,
            resource_name=f"Export {job.job_type.value}",
            old_values={"status": job.status.value, "job_type": job.job_type.value},
            success=True,
            severity=AuditSeverity.MEDIUM,
        )

        logger.info(f"Export job {job_id} deleted by {current_user.username}")

        return MessageResponse(message="Export job deleted successfully")

    except Exception as e:
        logger.error(f"Error deleting export job {job_id}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to delete export job"
        )


@router.post("/{job_id}/actions", response_model=MessageResponse)
async def perform_job_action(
        job_id: int,
        action: ExportJobAction,
        db: DatabaseDep,
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.EXPORT_EXECUTE)),
) -> MessageResponse:
    """
    Perform action on export job (cancel, retry, etc.).

    Args:
        job_id: Export job ID
        action: Action to perform
        db: Database session
        current_user: Current authenticated user

    Returns:
        Action confirmation
    """
    # Get job
    job = await export_crud.get(db, job_id)
    if not job:
        raise ResourceNotFoundException("Export job", job_id)

    # Check ownership
    if not current_user.is_superuser and job.user_id != current_user.id:
        raise ResourceNotFoundException("Export job", job_id)

    try:
        if action.action == "cancel":
            updated_job = await export_crud.cancel_job(db, job_id, action.reason or "User cancelled")
            if updated_job:
                message = "Export job cancelled successfully"
            else:
                message = "Job cannot be cancelled in current state"

        elif action.action == "retry":
            updated_job = await export_crud.retry_failed_job(db, job_id)
            if updated_job:
                message = "Export job queued for retry"
            else:
                message = "Job cannot be retried"

        else:
            raise ValidationException(f"Unknown action: {action.action}")

        # Log action
        await audit_crud.create_log(
            db=db,
            action=AuditAction.EXECUTE,
            resource_type=ResourceType.EXPORTS,
            user_id=current_user.id,
            resource_id=job_id,
            resource_name=f"Export {job.job_type.value}",
            new_values={"action": action.action, "reason": action.reason},
            success=True,
            severity=AuditSeverity.LOW,
        )

        logger.info(f"Action {action.action} performed on export job {job_id} by {current_user.username}")

        return MessageResponse(message=message)

    except ValidationException:
        raise
    except Exception as e:
        logger.error(f"Error performing action on export job {job_id}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to perform action"
        )


@router.get("/{job_id}/download")
async def download_export_file(
        job_id: int,
        db: DatabaseDep,
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.EXPORT_READ)),
) -> FileResponse:
    """
    Download export file.

    Args:
        job_id: Export job ID
        db: Database session
        current_user: Current authenticated user

    Returns:
        File response
    """
    # Get job
    job = await export_crud.get(db, job_id)
    if not job:
        raise ResourceNotFoundException("Export job", job_id)

    # Check ownership
    if not current_user.is_superuser and job.user_id != current_user.id:
        raise ResourceNotFoundException("Export job", job_id)

    # Check if job is completed
    if job.status != JobStatus.COMPLETED:
        raise ValidationException("Export job is not completed")

    # Check if file exists
    import os
    if not job.output_path or not os.path.exists(job.output_path):
        raise ResourceNotFoundException("Export file", "")

    try:
        # Log download
        await audit_crud.create_log(
            db=db,
            action=AuditAction.EXPORT_DOWNLOAD,
            resource_type=ResourceType.EXPORTS,
            user_id=current_user.id,
            resource_id=job_id,
            resource_name=job.output_filename or "export_file",
            success=True,
            severity=AuditSeverity.LOW,
        )

        # Return file
        return FileResponse(
            path=job.output_path,
            filename=job.output_filename or f"export_{job_id}.zip",
            media_type="application/octet-stream",
        )

    except Exception as e:
        logger.error(f"Error downloading export file {job_id}: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to download export file"
        )


@router.get("/{job_id}/progress", response_model=ExportProgress)
async def get_export_progress(
        job_id: int,
        db: DatabaseDep,
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.EXPORT_READ)),
) -> ExportProgress:
    """
    Get real-time export progress.

    Args:
        job_id: Export job ID
        db: Database session
        current_user: Current authenticated user

    Returns:
        Export progress information
    """
    # Get job
    job = await export_crud.get(db, job_id)
    if not job:
        raise ResourceNotFoundException("Export job", job_id)

    # Check ownership
    if not current_user.is_superuser and job.user_id != current_user.id:
        raise ResourceNotFoundException("Export job", job_id)

    # Calculate processing speed
    speed = None
    if job.started_at and job.records_processed > 0:
        from datetime import datetime
        elapsed = (datetime.utcnow() - job.started_at).total_seconds()
        if elapsed > 0:
            speed = job.records_processed / elapsed

    return ExportProgress(
        job_id=job.id,
        status=job.status,
        progress_percentage=job.progress_percentage,
        records_processed=job.records_processed,
        total_records=job.total_records,
        current_phase="processing",  # Would be tracked in real implementation
        estimated_completion=job.estimated_completion,
        speed_records_per_second=speed,
        memory_usage_mb=job.memory_usage_mb,
    )


# Bulk Operations
@router.post("/bulk", response_model=BulkExportResponse)
async def create_bulk_exports(
        bulk_request: BulkExportRequest,
        db: DatabaseDep,
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.EXPORT_CREATE)),
) -> BulkExportResponse:
    """
    Create multiple export jobs.

    Args:
        bulk_request: Bulk export request
        db: Database session
        current_user: Current authenticated user

    Returns:
        Bulk export response
    """
    try:
        import uuid
        from datetime import datetime

        batch_id = str(uuid.uuid4())
        job_ids = []

        for export_config in bulk_request.exports:
            # Create job configuration
            job_config = export_config.model_dump(exclude={"job_type", "priority"})

            # Create export job
            job = await export_crud.create_export_job(
                db=db,
                user_id=current_user.id,
                job_type=export_config.job_type,
                job_config=job_config,
                priority=export_config.priority,
            )
            job_ids.append(job.id)

        # Log bulk creation
        await audit_crud.create_log(
            db=db,
            action=AuditAction.EXPORT_CREATE,
            resource_type=ResourceType.EXPORTS,
            user_id=current_user.id,
            resource_name="bulk_export",
            new_values={
                "batch_id": batch_id,
                "job_count": len(job_ids),
                "batch_name": bulk_request.batch_name,
            },
            success=True,
            severity=AuditSeverity.MEDIUM,
        )

        logger.info(f"Bulk export batch {batch_id} created with {len(job_ids)} jobs by {current_user.username}")

        return BulkExportResponse(
            batch_id=batch_id,
            job_ids=job_ids,
            batch_name=bulk_request.batch_name,
            total_jobs=len(job_ids),
            estimated_completion=None,  # Would calculate based on queue
        )

    except Exception as e:
        logger.error(f"Error creating bulk exports: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to create bulk exports"
        )


# Queue and Statistics
@router.get("/queue/status", response_model=QueueStatus)
async def get_queue_status(
        db: DatabaseDep,
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.EXPORT_READ)),
) -> QueueStatus:
    """
    Get export queue status.

    Args:
        db: Database session
        current_user: Current authenticated user

    Returns:
        Queue status information
    """
    try:
        queue_status = await export_crud.get_queue_status(db)
        return QueueStatus(**queue_status)

    except Exception as e:
        logger.error(f"Error getting queue status: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve queue status"
        )


@router.get("/statistics", response_model=ExportStatistics)
async def get_export_statistics(
        db: DatabaseDep,
        days: int = Query(30, ge=1, le=365, description="Number of days to analyze"),
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.EXPORT_READ)),
) -> ExportStatistics:
    """
    Get export statistics.

    Args:
        days: Number of days to analyze
        db: Database session
        current_user: Current authenticated user

    Returns:
        Export statistics
    """
    try:
        stats = await export_crud.get_job_statistics(db)

        return ExportStatistics(
            total_jobs=stats.get("total", 0),
            by_status={
                JobStatus.PENDING.value: stats.get(JobStatus.PENDING.value, 0),
                JobStatus.PROCESSING.value: stats.get(JobStatus.PROCESSING.value, 0),
                JobStatus.COMPLETED.value: stats.get(JobStatus.COMPLETED.value, 0),
                JobStatus.FAILED.value: stats.get(JobStatus.FAILED.value, 0),
                JobStatus.CANCELLED.value: stats.get(JobStatus.CANCELLED.value, 0),
            },
            by_job_type={
                JobType.PDF_REPORT.value: stats.get(f"{JobType.PDF_REPORT.value}_count", 0),
                JobType.CSV_EXPORT.value: stats.get(f"{JobType.CSV_EXPORT.value}_count", 0),
                JobType.EXCEL_EXPORT.value: stats.get(f"{JobType.EXCEL_EXPORT.value}_count", 0),
                JobType.DATABASE_BACKUP.value: stats.get(f"{JobType.DATABASE_BACKUP.value}_count", 0),
            },
            by_user={},  # Would need additional implementation
            avg_processing_time_seconds=stats.get("avg_processing_time_seconds", 0),
            jobs_last_24h=stats.get("jobs_last_24h", 0),
            total_output_size_bytes=0,  # Would need additional calculation
            success_rate_percentage=0.0,  # Would need additional calculation
            retry_rate_percentage=0.0,  # Would need additional calculation
        )

    except Exception as e:
        logger.error(f"Error getting export statistics: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve export statistics"
        )


@router.get("/policy", response_model=DataExportPolicy)
async def get_export_policy(
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.EXPORT_READ)),
) -> DataExportPolicy:
    """
    Get data export policy and restrictions.

    Args:
        current_user: Current authenticated user

    Returns:
        Export policy information
    """
    return DataExportPolicy(
        max_concurrent_jobs=5,
        max_records_per_export=100000,
        max_file_size_mb=1000,
        retention_days=30,
        allowed_formats=["csv", "excel", "pdf", "json"],
        require_approval=False,
        rate_limit_per_hour=10,
        quota_reset_time="00:00",
    )


# Maintenance
@router.post("/maintenance/cleanup", response_model=MessageResponse)
async def cleanup_expired_exports(
        db: DatabaseDep,
        current_user: CurrentUser = Depends(require_permissions(SystemPermissions.EXPORT_MANAGE)),
) -> MessageResponse:
    """
    Clean up expired export files and jobs.

    Args:
        db: Database session
        current_user: Current authenticated user

    Returns:
        Cleanup confirmation
    """
    try:
        cleaned_count = await export_crud.cleanup_expired_jobs(db)

        # Log cleanup
        await audit_crud.create_log(
            db=db,
            action=AuditAction.MAINTENANCE,
            resource_type=ResourceType.EXPORTS,
            user_id=current_user.id,
            resource_name="export_cleanup",
            new_values={"cleaned_jobs": cleaned_count},
            success=True,
            severity=AuditSeverity.LOW,
        )

        logger.info(f"Export cleanup completed: {cleaned_count} jobs cleaned by {current_user.username}")

        return MessageResponse(message=f"Cleaned up {cleaned_count} expired export jobs")

    except Exception as e:
        logger.error(f"Error in export cleanup: {str(e)}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Export cleanup failed"
        )
