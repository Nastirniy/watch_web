"""
Export job CRUD operations for data export functionality.
Handles export job management, progress tracking, and file generation.
"""

from datetime import datetime, timedelta
from typing import Dict, List, Optional

from loguru import logger
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.database.crud.base import CRUDBase
from backend.database.models import ExportJob, JobType, JobStatus
from backend.api.schemas import ExportJobCreate, ExportJobUpdate


class CRUDExportJob(CRUDBase[ExportJob, ExportJobCreate, ExportJobUpdate]):
    """CRUD operations for ExportJob model."""

    async def get_user_jobs(
            self,
            db: AsyncSession,
            user_id: int,
            status: Optional[JobStatus] = None,
            limit: int = 50
    ) -> List[ExportJob]:
        """Get export jobs for a specific user."""
        try:
            query = (
                select(ExportJob)
                .where(ExportJob.user_id == user_id)
            )

            if status:
                query = query.where(ExportJob.status == status)

            query = query.order_by(ExportJob.created_at.desc()).limit(limit)

            result = await db.execute(query)
            return result.scalars().all()
        except Exception as e:
            logger.error(f"Error getting user jobs for user {user_id}: {e}")
            raise

    async def get_pending_jobs(self, db: AsyncSession) -> List[ExportJob]:
        """Get all pending export jobs ordered by priority and creation time."""
        try:
            query = (
                select(ExportJob)
                .where(ExportJob.status == JobStatus.PENDING)
                .order_by(ExportJob.priority.desc(), ExportJob.created_at.asc())
            )
            result = await db.execute(query)
            return result.scalars().all()
        except Exception as e:
            logger.error(f"Error getting pending jobs: {e}")
            raise

    async def get_running_jobs(self, db: AsyncSession) -> List[ExportJob]:
        """Get all currently running export jobs."""
        try:
            query = (
                select(ExportJob)
                .where(ExportJob.status == JobStatus.PROCESSING)
                .order_by(ExportJob.started_at.asc())
            )
            result = await db.execute(query)
            return result.scalars().all()
        except Exception as e:
            logger.error(f"Error getting running jobs: {e}")
            raise

    async def create_export_job(
            self,
            db: AsyncSession,
            user_id: int,
            job_type: JobType,
            job_config: Dict,
            priority: int = 0
    ) -> ExportJob:
        """Create a new export job with configuration."""
        try:
            job_data = {
                "user_id": user_id,
                "job_type": job_type,
                "priority": priority,
                **job_config
            }

            job = ExportJob(**job_data)
            db.add(job)
            await db.commit()
            await db.refresh(job)

            logger.info(f"Created export job {job.id} of type {job_type.value} for user {user_id}")
            return job

        except Exception as e:
            await db.rollback()
            logger.error(f"Error creating export job: {e}")
            raise

    async def start_job(self, db: AsyncSession, job_id: int) -> Optional[ExportJob]:
        """Mark job as started and update status."""
        try:
            job = await self.get(db, job_id)
            if job and job.status == JobStatus.PENDING:
                job.start_processing()
                await db.commit()
                await db.refresh(job)
                logger.info(f"Started export job {job_id}")
                return job
            return None
        except Exception as e:
            await db.rollback()
            logger.error(f"Error starting job {job_id}: {e}")
            raise

    async def update_progress(
            self,
            db: AsyncSession,
            job_id: int,
            percentage: int,
            records_processed: Optional[int] = None
    ) -> Optional[ExportJob]:
        """Update job progress."""
        try:
            job = await self.get(db, job_id)
            if job and job.status == JobStatus.PROCESSING:
                job.update_progress(percentage, records_processed)
                await db.commit()
                await db.refresh(job)
                return job
            return None
        except Exception as e:
            await db.rollback()
            logger.error(f"Error updating progress for job {job_id}: {e}")
            raise

    async def complete_job(
            self,
            db: AsyncSession,
            job_id: int,
            output_filename: str,
            output_path: str,
            output_size: int,
            output_hash: Optional[str] = None
    ) -> Optional[ExportJob]:
        """Mark job as completed successfully."""
        try:
            job = await self.get(db, job_id)
            if job and job.status == JobStatus.PROCESSING:
                job.complete_successfully(output_filename, output_path, output_size)
                if output_hash:
                    job.output_hash = output_hash
                await db.commit()
                await db.refresh(job)
                logger.info(f"Completed export job {job_id}")
                return job
            return None
        except Exception as e:
            await db.rollback()
            logger.error(f"Error completing job {job_id}: {e}")
            raise

    async def fail_job(
            self,
            db: AsyncSession,
            job_id: int,
            error_message: str,
            error_details: Optional[Dict] = None
    ) -> Optional[ExportJob]:
        """Mark job as failed with error details."""
        try:
            job = await self.get(db, job_id)
            if job and job.is_running:
                job.fail_with_error(error_message, error_details)
                await db.commit()
                await db.refresh(job)
                logger.error(f"Failed export job {job_id}: {error_message}")
                return job
            return None
        except Exception as e:
            await db.rollback()
            logger.error(f"Error failing job {job_id}: {e}")
            raise

    async def cancel_job(
            self,
            db: AsyncSession,
            job_id: int,
            reason: str = "User cancelled"
    ) -> Optional[ExportJob]:
        """Cancel an export job."""
        try:
            job = await self.get(db, job_id)
            if job and job.is_running:
                job.cancel(reason)
                await db.commit()
                await db.refresh(job)
                logger.info(f"Cancelled export job {job_id}: {reason}")
                return job
            return None
        except Exception as e:
            await db.rollback()
            logger.error(f"Error cancelling job {job_id}: {e}")
            raise

    async def retry_failed_job(self, db: AsyncSession, job_id: int) -> Optional[ExportJob]:
        """Retry a failed export job."""
        try:
            job = await self.get(db, job_id)
            if job and job.status == JobStatus.FAILED and job.retry_count < job.max_retries:
                job.status = JobStatus.PENDING
                job.retry_count += 1
                job.error_message = None
                job.error_details = None
                job.started_at = None
                job.completed_at = None

                await db.commit()
                await db.refresh(job)
                logger.info(f"Retrying export job {job_id} (attempt {job.retry_count})")
                return job
            return None
        except Exception as e:
            await db.rollback()
            logger.error(f"Error retrying job {job_id}: {e}")
            raise

    async def cleanup_expired_jobs(self, db: AsyncSession) -> int:
        """Clean up expired export jobs and their files."""
        try:
            from sqlalchemy import delete
            import os

            # Find expired jobs
            query = (
                select(ExportJob)
                .where(
                    and_(
                        ExportJob.expires_at <= datetime.utcnow(),
                        ExportJob.status == JobStatus.COMPLETED
                    )
                )
            )
            result = await db.execute(query)
            expired_jobs = result.scalars().all()

            cleaned_count = 0
            for job in expired_jobs:
                # Delete output file if it exists
                if job.output_path and os.path.exists(job.output_path):
                    try:
                        os.remove(job.output_path)
                        logger.debug(f"Deleted expired file: {job.output_path}")
                    except Exception as e:
                        logger.warning(f"Could not delete file {job.output_path}: {e}")

                # Update job status
                job.status = JobStatus.EXPIRED
                cleaned_count += 1

            if cleaned_count > 0:
                await db.commit()
                logger.info(f"Cleaned up {cleaned_count} expired export jobs")

            return cleaned_count

        except Exception as e:
            await db.rollback()
            logger.error(f"Error cleaning up expired jobs: {e}")
            raise

    async def get_job_statistics(self, db: AsyncSession) -> Dict[str, int]:
        """Get export job statistics."""
        try:
            stats = {}

            # Count by status
            for status in JobStatus:
                query = select(func.count()).where(ExportJob.status == status)
                result = await db.execute(query)
                stats[status.value] = result.scalar()

            # Count by job type
            for job_type in JobType:
                query = select(func.count()).where(ExportJob.job_type == job_type)
                result = await db.execute(query)
                stats[f'{job_type.value}_count'] = result.scalar()

            # Average processing time for completed jobs
            query = (
                select(func.avg(
                    func.extract('epoch', ExportJob.completed_at - ExportJob.started_at)
                ))
                .where(
                    and_(
                        ExportJob.status == JobStatus.COMPLETED,
                        ExportJob.started_at.isnot(None),
                        ExportJob.completed_at.isnot(None)
                    )
                )
            )
            result = await db.execute(query)
            avg_duration = result.scalar()
            stats['avg_processing_time_seconds'] = round(avg_duration, 2) if avg_duration else 0

            # Jobs in last 24 hours
            cutoff_time = datetime.utcnow() - timedelta(hours=24)
            query = select(func.count()).where(ExportJob.created_at >= cutoff_time)
            result = await db.execute(query)
            stats['jobs_last_24h'] = result.scalar()

            return stats

        except Exception as e:
            logger.error(f"Error getting job statistics: {e}")
            raise

    async def get_queue_status(self, db: AsyncSession) -> Dict[str, any]:
        """Get export queue status information."""
        try:
            # Count pending jobs by priority
            pending_by_priority = {}
            for priority in range(0, 10):  # Assuming priority 0-9
                query = (
                    select(func.count())
                    .where(
                        and_(
                            ExportJob.status == JobStatus.PENDING,
                            ExportJob.priority == priority
                        )
                    )
                )
                result = await db.execute(query)
                count = result.scalar()
                if count > 0:
                    pending_by_priority[f'priority_{priority}'] = count

            # Currently processing jobs
            query = select(func.count()).where(ExportJob.status == JobStatus.PROCESSING)
            result = await db.execute(query)
            processing_count = result.scalar()

            # Estimated wait time (simplified calculation)
            if processing_count > 0:
                # Get average processing time
                avg_time_query = (
                    select(func.avg(
                        func.extract('epoch', ExportJob.completed_at - ExportJob.started_at)
                    ))
                    .where(
                        and_(
                            ExportJob.status == JobStatus.COMPLETED,
                            ExportJob.completed_at >= datetime.utcnow() - timedelta(days=7)
                        )
                    )
                )
                result = await db.execute(avg_time_query)
                avg_processing_time = result.scalar() or 300  # Default 5 minutes

                pending_count = sum(pending_by_priority.values())
                estimated_wait_seconds = avg_processing_time * pending_count
            else:
                estimated_wait_seconds = 0

            return {
                'pending_by_priority': pending_by_priority,
                'processing_count': processing_count,
                'estimated_wait_seconds': round(estimated_wait_seconds),
                'estimated_wait_formatted': self._format_duration(estimated_wait_seconds)
            }

        except Exception as e:
            logger.error(f"Error getting queue status: {e}")
            raise

    def _format_duration(self, seconds: float) -> str:
        """Format duration in human-readable format."""
        if seconds < 60:
            return f"{int(seconds)} seconds"
        elif seconds < 3600:
            minutes = int(seconds / 60)
            return f"{minutes} minutes"
        else:
            hours = int(seconds / 3600)
            minutes = int((seconds % 3600) / 60)
            return f"{hours}h {minutes}m"


# Create instance
export_job = CRUDExportJob(ExportJob)
