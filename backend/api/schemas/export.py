"""
Export job schemas for data export and reporting functionality.
Handles export job creation, monitoring, and file generation.
"""

from datetime import datetime, date
from typing import Dict, List, Optional, Any

from pydantic import Field, field_validator

from backend.database.models import JobType, JobStatus
from backend.api.schemas.base import BaseSchema, TimestampSchema, SearchFilters, DateRangeFilter


class ExportJobBase(BaseSchema):
    """Base export job schema."""

    job_type: JobType = Field(description="Type of export job")
    name: Optional[str] = Field(None, description="Custom job name")
    description: Optional[str] = Field(None, description="Job description")
    date_from: Optional[date] = Field(None, description="Start date for data export")
    date_to: Optional[date] = Field(None, description="End date for data export")
    include_media: bool = Field(default=False, description="Include media files in export")
    include_deleted: bool = Field(default=False, description="Include soft-deleted records")
    export_format: str = Field(default="standard", description="Export format variant")
    compression: bool = Field(default=True, description="Compress output file")
    priority: int = Field(default=0, description="Job priority (higher = more priority)")


class ExportJobCreate(ExportJobBase):
    """Schema for creating export jobs."""

    # Filter parameters
    post_ids: Optional[List[int]] = Field(None, description="Post IDs to include")
    signal_type_ids: Optional[List[int]] = Field(None, description="Signal type IDs to include")
    user_ids: Optional[List[int]] = Field(None, description="User IDs to include")
    frequency_min: Optional[float] = Field(None, description="Minimum frequency filter (MHz)")
    frequency_max: Optional[float] = Field(None, description="Maximum frequency filter (MHz)")
    confidence_levels: Optional[List[str]] = Field(None, description="Confidence levels to include")
    observation_statuses: Optional[List[str]] = Field(None, description="Observation statuses to include")

    @field_validator('date_to')
    @classmethod
    def validate_date_range(cls, v: Optional[date], info) -> Optional[date]:
        if v and 'date_from' in info.data and info.data['date_from']:
            if v < info.data['date_from']:
                raise ValueError("End date must be after start date")
        return v

    @field_validator('priority')
    @classmethod
    def validate_priority(cls, v: int) -> int:
        if v < 0 or v > 10:
            raise ValueError("Priority must be between 0 and 10")
        return v


class ExportJobUpdate(BaseSchema):
    """Schema for updating export jobs."""

    name: Optional[str] = Field(None, description="Custom job name")
    description: Optional[str] = Field(None, description="Job description")
    priority: Optional[int] = Field(None, description="Job priority")


class ExportJob(ExportJobBase, TimestampSchema):
    """Export job response schema."""

    id: int = Field(description="Export job ID")
    user_id: int = Field(description="Job creator user ID")
    status: JobStatus = Field(description="Job status")

    # Execution tracking
    started_at: Optional[datetime] = Field(None, description="Job start timestamp")
    completed_at: Optional[datetime] = Field(None, description="Job completion timestamp")
    expires_at: Optional[datetime] = Field(None, description="Output file expiration")

    # Progress tracking
    progress_percentage: int = Field(description="Job progress (0-100)")
    records_processed: int = Field(description="Number of records processed")
    total_records: Optional[int] = Field(None, description="Total records to process")

    # Output information
    output_filename: Optional[str] = Field(None, description="Generated output filename")
    output_size: Optional[int] = Field(None, description="Output file size in bytes")
    output_hash: Optional[str] = Field(None, description="Output file SHA-256 hash")

    # Error handling
    error_message: Optional[str] = Field(None, description="Error message if job failed")
    retry_count: int = Field(description="Number of retry attempts")
    max_retries: int = Field(description="Maximum retry attempts")

    # Resource usage
    memory_usage_mb: Optional[float] = Field(None, description="Peak memory usage in MB")
    cpu_time_seconds: Optional[float] = Field(None, description="CPU time consumed")

    # Related data
    creator_username: Optional[str] = Field(None, description="Job creator username")
    download_url: Optional[str] = Field(None, description="Download URL for completed job")

    @property
    def is_running(self) -> bool:
        """Check if job is currently running."""
        return self.status in [JobStatus.PENDING, JobStatus.PROCESSING]

    @property
    def duration_seconds(self) -> Optional[float]:
        """Get job duration in seconds."""
        if self.started_at and self.completed_at:
            return (self.completed_at - self.started_at).total_seconds()
        return None

    @property
    def output_size_formatted(self) -> Optional[str]:
        """Get human-readable output file size."""
        if not self.output_size:
            return None

        size = self.output_size
        units = ['B', 'KB', 'MB', 'GB']
        unit_index = 0

        while size >= 1024 and unit_index < len(units) - 1:
            size /= 1024
            unit_index += 1

        return f"{size:.2f} {units[unit_index]}"


class ExportJobSummary(BaseSchema):
    """Simplified export job schema for listings."""

    id: int = Field(description="Export job ID")
    job_type: JobType = Field(description="Job type")
    name: Optional[str] = Field(None, description="Job name")
    status: JobStatus = Field(description="Job status")
    progress_percentage: int = Field(description="Progress percentage")
    created_at: datetime = Field(description="Creation timestamp")
    started_at: Optional[datetime] = Field(None, description="Start timestamp")
    completed_at: Optional[datetime] = Field(None, description="Completion timestamp")
    output_size: Optional[int] = Field(None, description="Output file size")
    creator_username: Optional[str] = Field(None, description="Creator username")


class ExportSearchFilters(SearchFilters, DateRangeFilter):
    """Export job search filters."""

    user_ids: Optional[List[int]] = Field(None, description="Filter by creator user IDs")
    job_types: Optional[List[JobType]] = Field(None, description="Filter by job types")
    statuses: Optional[List[JobStatus]] = Field(None, description="Filter by job statuses")
    priority_min: Optional[int] = Field(None, description="Minimum priority")
    priority_max: Optional[int] = Field(None, description="Maximum priority")
    created_after: Optional[datetime] = Field(None, description="Created after timestamp")
    created_before: Optional[datetime] = Field(None, description="Created before timestamp")
    has_errors: Optional[bool] = Field(None, description="Filter by error status")


class ExportTemplate(BaseSchema):
    """Export template for predefined export configurations."""

    id: int = Field(description="Template ID")
    name: str = Field(description="Template name")
    description: Optional[str] = Field(None, description="Template description")
    job_type: JobType = Field(description="Export job type")
    is_public: bool = Field(description="Public template")
    created_by: int = Field(description="Creator user ID")
    config: Dict[str, Any] = Field(description="Template configuration")
    usage_count: int = Field(default=0, description="Number of times used")
    created_at: datetime = Field(description="Creation timestamp")


class ExportTemplateCreate(BaseSchema):
    """Schema for creating export templates."""

    name: str = Field(description="Template name")
    description: Optional[str] = Field(None, description="Template description")
    job_type: JobType = Field(description="Export job type")
    is_public: bool = Field(default=False, description="Make template public")
    config: Dict[str, Any] = Field(description="Template configuration")


class QueueStatus(BaseSchema):
    """Export queue status information."""

    pending_jobs: int = Field(description="Number of pending jobs")
    processing_jobs: int = Field(description="Number of processing jobs")
    pending_by_priority: Dict[str, int] = Field(description="Pending jobs by priority")
    estimated_wait_seconds: int = Field(description="Estimated wait time")
    estimated_wait_formatted: str = Field(description="Formatted wait time")
    average_processing_time: float = Field(description="Average processing time")
    queue_health: str = Field(description="Queue health status")


class ExportStatistics(BaseSchema):
    """Export job statistics."""

    total_jobs: int = Field(description="Total number of jobs")
    by_status: Dict[str, int] = Field(description="Count by status")
    by_job_type: Dict[str, int] = Field(description="Count by job type")
    by_user: Dict[str, int] = Field(description="Count by user")
    avg_processing_time_seconds: float = Field(description="Average processing time")
    jobs_last_24h: int = Field(description="Jobs created in last 24 hours")
    total_output_size_bytes: int = Field(description="Total output size")
    success_rate_percentage: float = Field(description="Success rate percentage")
    retry_rate_percentage: float = Field(description="Retry rate percentage")


class ExportProgress(BaseSchema):
    """Real-time export progress information."""

    job_id: int = Field(description="Job ID")
    status: JobStatus = Field(description="Current status")
    progress_percentage: int = Field(description="Progress percentage")
    records_processed: int = Field(description="Records processed")
    total_records: Optional[int] = Field(None, description="Total records")
    current_phase: str = Field(description="Current processing phase")
    estimated_completion: Optional[datetime] = Field(None, description="Estimated completion time")
    speed_records_per_second: Optional[float] = Field(None, description="Processing speed")
    memory_usage_mb: Optional[float] = Field(None, description="Current memory usage")


class BulkExportRequest(BaseSchema):
    """Bulk export request for multiple datasets."""

    exports: List[ExportJobCreate] = Field(description="List of export job configurations")
    batch_name: Optional[str] = Field(None, description="Batch name")
    combine_outputs: bool = Field(default=False, description="Combine all outputs into single file")
    schedule_at: Optional[datetime] = Field(None, description="Schedule exports for later")


class BulkExportResponse(BaseSchema):
    """Bulk export response."""

    batch_id: str = Field(description="Batch ID")
    job_ids: List[int] = Field(description="Created job IDs")
    batch_name: Optional[str] = Field(None, description="Batch name")
    total_jobs: int = Field(description="Total number of jobs")
    estimated_completion: Optional[datetime] = Field(None, description="Estimated completion time")


class ExportJobAction(BaseSchema):
    """Export job action request."""

    action: str = Field(description="Action to perform (cancel, retry, delete)")
    reason: Optional[str] = Field(None, description="Reason for action")


class DataExportPolicy(BaseSchema):
    """Data export policy and restrictions."""

    max_concurrent_jobs: int = Field(description="Maximum concurrent jobs per user")
    max_records_per_export: int = Field(description="Maximum records per export")
    max_file_size_mb: int = Field(description="Maximum output file size")
    retention_days: int = Field(description="Export file retention period")
    allowed_formats: List[str] = Field(description="Allowed export formats")
    require_approval: bool = Field(description="Require approval for large exports")
    rate_limit_per_hour: int = Field(description="Rate limit per user per hour")
    quota_reset_time: str = Field(description="Quota reset time")


class ExportNotification(BaseSchema):
    """Export completion notification."""

    job_id: int = Field(description="Job ID")
    job_type: JobType = Field(description="Job type")
    status: JobStatus = Field(description="Final status")
    download_url: Optional[str] = Field(None, description="Download URL")
    expires_at: Optional[datetime] = Field(None, description="Download expiration")
    file_size: Optional[int] = Field(None, description="File size")
    processing_time: Optional[float] = Field(None, description="Processing time")
    error_message: Optional[str] = Field(None, description="Error message if failed")


class ScheduledExport(BaseSchema):
    """Scheduled export configuration."""

    id: int = Field(description="Scheduled export ID")
    name: str = Field(description="Schedule name")
    description: Optional[str] = Field(None, description="Description")
    is_active: bool = Field(description="Schedule active status")
    cron_expression: str = Field(description="Cron expression for scheduling")
    job_config: ExportJobCreate = Field(description="Export job configuration")
    last_run: Optional[datetime] = Field(None, description="Last execution time")
    next_run: Optional[datetime] = Field(None, description="Next scheduled execution")
    created_by: int = Field(description="Creator user ID")
    notification_emails: List[str] = Field(default_factory=list, description="Notification email addresses")
    max_retries: int = Field(default=3, description="Maximum retry attempts")
    timeout_minutes: int = Field(default=60, description="Job timeout in minutes")
