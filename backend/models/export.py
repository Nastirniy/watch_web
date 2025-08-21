"""
Export job models for data export functionality.
Handles various export formats and job tracking.
"""

from datetime import datetime, date
from enum import Enum
from typing import List, Optional, Dict, Any, TYPE_CHECKING

from sqlmodel import Field, Relationship, JSON

from backend.models.base import BaseModel

if TYPE_CHECKING:
    from backend.models.user import User


class JobType(str, Enum):
    """Export job type enumeration."""
    PDF_REPORT = "pdf_report"
    CSV_EXPORT = "csv_export"
    EXCEL_EXPORT = "excel_export"
    DATABASE_BACKUP = "database_backup"
    STATISTICS_REPORT = "statistics_report"
    JSON_EXPORT = "json_export"


class JobStatus(str, Enum):
    """Export job status enumeration."""
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class ExportJob(BaseModel, table=True):
    """Export job tracking and metadata."""

    __tablename__ = "export_jobs"

    id: Optional[int] = Field(default=None, primary_key=True)

    # Job ownership
    user_id: int = Field(foreign_key="users.id", index=True, description="Job creator user ID")

    # Job configuration
    job_type: JobType = Field(index=True, description="Type of export job")
    name: Optional[str] = Field(default=None, max_length=255, description="Custom job name")
    description: Optional[str] = Field(default=None, description="Job description")

    # Export parameters
    date_from: Optional[date] = Field(default=None, description="Start date for data export")
    date_to: Optional[date] = Field(default=None, description="End date for data export")

    # Filter parameters (stored as JSON)
    post_ids: Optional[List[int]] = Field(default=None, sa_column=JSON, description="Post IDs to include")
    signal_type_ids: Optional[List[int]] = Field(default=None, sa_column=JSON, description="Signal type IDs to include")
    user_ids: Optional[List[int]] = Field(default=None, sa_column=JSON, description="User IDs to include")

    # Advanced filters
    frequency_min: Optional[float] = Field(default=None, description="Minimum frequency filter (MHz)")
    frequency_max: Optional[float] = Field(default=None, description="Maximum frequency filter (MHz)")
    confidence_levels: Optional[List[str]] = Field(default=None, sa_column=JSON, description="Confidence levels to include")
    observation_statuses: Optional[List[str]] = Field(default=None, sa_column=JSON, description="Observation statuses to include")

    # Export options
    include_media: bool = Field(default=False, description="Include media files in export")
    include_deleted: bool = Field(default=False, description="Include soft-deleted records")
    export_format: str = Field(default="standard", description="Export format variant")
    compression: bool = Field(default=True, description="Compress output file")

    # Job status and timing
    status: JobStatus = Field(default=JobStatus.PENDING, index=True, description="Job status")
    priority: int = Field(default=0, description="Job priority (higher = more priority)")

    # Execution tracking
    started_at: Optional[datetime] = Field(default=None, description="Job start timestamp")
    completed_at: Optional[datetime] = Field(default=None, description="Job completion timestamp")
    expires_at: Optional[datetime] = Field(default=None, index=True, description="Output file expiration")

    # Progress tracking
    progress_percentage: int = Field(default=0, description="Job progress (0-100)")
    records_processed: int = Field(default=0, description="Number of records processed")
    total_records: Optional[int] = Field(default=None, description="Total records to process")

    # Output information
    output_filename: Optional[str] = Field(default=None, description="Generated output filename")
    output_path: Optional[str] = Field(default=None, description="Output file path")
    output_size: Optional[int] = Field(default=None, description="Output file size in bytes")
    output_hash: Optional[str] = Field(default=None, description="Output file SHA-256 hash")

    # Error handling
    error_message: Optional[str] = Field(default=None, description="Error message if job failed")
    error_details: Optional[Dict[str, Any]] = Field(default=None, sa_column=JSON, description="Detailed error information")
    retry_count: int = Field(default=0, description="Number of retry attempts")
    max_retries: int = Field(default=3, description="Maximum retry attempts")

    # Resource usage
    memory_usage_mb: Optional[float] = Field(default=None, description="Peak memory usage in MB")
    cpu_time_seconds: Optional[float] = Field(default=None, description="CPU time consumed")

    # Relationships
    user: "User" = Relationship(back_populates="export_jobs")

    @property
    def is_running(self) -> bool:
        """Check if job is currently running."""
        return self.status in [JobStatus.PENDING, JobStatus.PROCESSING]

    @property
    def is_completed(self) -> bool:
        """Check if job completed successfully."""
        return self.status == JobStatus.COMPLETED

    @property
    def is_failed(self) -> bool:
        """Check if job failed."""
        return self.status == JobStatus.FAILED

    @property
    def duration(self) -> Optional[float]:
        """Get job duration in seconds."""
        if self.started_at and self.completed_at:
            return (self.completed_at - self.started_at).total_seconds()
        return None

    @property
    def estimated_completion(self) -> Optional[datetime]:
        """Estimate job completion time based on progress."""
        if (self.status != JobStatus.PROCESSING or
            not self.started_at or
            self.progress_percentage <= 0):
            return None

        elapsed = (datetime.utcnow() - self.started_at).total_seconds()
        estimated_total = elapsed * 100 / self.progress_percentage
        remaining = estimated_total - elapsed

        return datetime.utcnow() + datetime.timedelta(seconds=remaining)

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

    def start_processing(self) -> None:
        """Mark job as started."""
        self.status = JobStatus.PROCESSING
        self.started_at = datetime.utcnow()
        self.progress_percentage = 0

    def update_progress(self, percentage: int, records_processed: Optional[int] = None) -> None:
        """Update job progress."""
        self.progress_percentage = min(100, max(0, percentage))
        if records_processed is not None:
            self.records_processed = records_processed

    def complete_successfully(self, output_filename: str, output_path: str, output_size: int) -> None:
        """Mark job as completed successfully."""
        self.status = JobStatus.COMPLETED
        self.completed_at = datetime.utcnow()
        self.progress_percentage = 100
        self.output_filename = output_filename
        self.output_path = output_path
        self.output_size = output_size

        # Set expiration time
        from datetime import timedelta
        from app.config import settings
        self.expires_at = datetime.utcnow() + timedelta(hours=settings.EXPORT_EXPIRE_HOURS)

    def fail_with_error(self, error_message: str, error_details: Optional[Dict[str, Any]] = None) -> None:
        """Mark job as failed with error details."""
        self.status = JobStatus.FAILED
        self.completed_at = datetime.utcnow()
        self.error_message = error_message
        self.error_details = error_details or {}

    def cancel(self, reason: Optional[str] = None) -> None:
        """Cancel the job."""
        self.status = JobStatus.CANCELLED
        self.completed_at = datetime.utcnow()
        if reason:
            self.error_message = f"Cancelled: {reason}"
