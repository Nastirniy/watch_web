"""
Audit log CRUD operations for security and compliance tracking.
Handles audit log creation, querying, and retention management.
"""

from datetime import datetime, timedelta, date
from typing import Dict, List, Optional, Any

from loguru import logger
from sqlalchemy import and_, func, or_, select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.database.crud.base import CRUDBase
from backend.database.models import AuditLog, AuditAction, ResourceType, AuditSeverity


class CRUDAuditLog(CRUDBase[AuditLog, dict, dict]):
    """CRUD operations for AuditLog model."""

    async def create_log(
            self,
            db: AsyncSession,
            action: AuditAction,
            resource_type: ResourceType,
            user_id: Optional[int] = None,
            session_id: Optional[str] = None,
            resource_id: Optional[int] = None,
            resource_name: Optional[str] = None,
            old_values: Optional[Dict[str, Any]] = None,
            new_values: Optional[Dict[str, Any]] = None,
            ip_address: Optional[str] = None,
            user_agent: Optional[str] = None,
            request_url: Optional[str] = None,
            request_method: Optional[str] = None,
            success: bool = True,
            error_message: Optional[str] = None,
            severity: AuditSeverity = AuditSeverity.LOW,
            **kwargs
    ) -> AuditLog:
        """Create a new audit log entry."""
        try:
            audit_log = AuditLog.create_log(
                action=action,
                resource_type=resource_type,
                user_id=user_id,
                session_id=session_id,
                resource_id=resource_id,
                resource_name=resource_name,
                old_values=old_values,
                new_values=new_values,
                ip_address=ip_address,
                user_agent=user_agent,
                request_url=request_url,
                request_method=request_method,
                success=success,
                error_message=error_message,
                severity=severity,
                **kwargs
            )

            db.add(audit_log)
            await db.commit()
            await db.refresh(audit_log)

            logger.debug(f"Created audit log {audit_log.id}: {audit_log.summary}")
            return audit_log

        except Exception as e:
            await db.rollback()
            logger.error(f"Error creating audit log: {e}")
            raise

    async def get_user_activity(
            self,
            db: AsyncSession,
            user_id: int,
            start_date: Optional[date] = None,
            end_date: Optional[date] = None,
            actions: Optional[List[AuditAction]] = None,
            limit: int = 100
    ) -> List[AuditLog]:
        """Get audit logs for a specific user."""
        try:
            query = (
                select(AuditLog)
                .where(AuditLog.user_id == user_id)
                .options(selectinload(AuditLog.user))
            )

            # Date filters
            if start_date:
                query = query.where(func.date(AuditLog.created_at) >= start_date)
            if end_date:
                query = query.where(func.date(AuditLog.created_at) <= end_date)

            # Action filters
            if actions:
                query = query.where(AuditLog.action.in_(actions))

            query = query.order_by(AuditLog.created_at.desc()).limit(limit)

            result = await db.execute(query)
            return result.scalars().all()

        except Exception as e:
            logger.error(f"Error getting user activity for user {user_id}: {e}")
            raise

    async def get_resource_history(
            self,
            db: AsyncSession,
            resource_type: ResourceType,
            resource_id: int,
            limit: int = 50
    ) -> List[AuditLog]:
        """Get audit history for a specific resource."""
        try:
            query = (
                select(AuditLog)
                .where(
                    and_(
                        AuditLog.resource_type == resource_type,
                        AuditLog.resource_id == resource_id
                    )
                )
                .options(selectinload(AuditLog.user))
                .order_by(AuditLog.created_at.desc())
                .limit(limit)
            )

            result = await db.execute(query)
            return result.scalars().all()

        except Exception as e:
            logger.error(f"Error getting resource history for {resource_type.value}:{resource_id}: {e}")
            raise

    async def get_security_events(
            self,
            db: AsyncSession,
            severity: Optional[AuditSeverity] = None,
            start_date: Optional[datetime] = None,
            end_date: Optional[datetime] = None,
            limit: int = 100
    ) -> List[AuditLog]:
        """Get security-related audit events."""
        try:
            # Security-related actions
            security_actions = [
                AuditAction.LOGIN,
                AuditAction.LOGIN_FAILED,
                AuditAction.LOGOUT,
                AuditAction.PASSWORD_CHANGE,
                AuditAction.PERMISSION_GRANT,
                AuditAction.PERMISSION_REVOKE,
                AuditAction.ROLE_ASSIGN,
                AuditAction.ROLE_REMOVE,
            ]

            query = (
                select(AuditLog)
                .where(
                    or_(
                        AuditLog.action.in_(security_actions),
                        AuditLog.severity.in_([AuditSeverity.HIGH, AuditSeverity.CRITICAL])
                    )
                )
                .options(selectinload(AuditLog.user))
            )

            # Date filters
            if start_date:
                query = query.where(AuditLog.created_at >= start_date)
            if end_date:
                query = query.where(AuditLog.created_at <= end_date)

            # Severity filter
            if severity:
                query = query.where(AuditLog.severity == severity)

            query = query.order_by(AuditLog.created_at.desc()).limit(limit)

            result = await db.execute(query)
            return result.scalars().all()

        except Exception as e:
            logger.error(f"Error getting security events: {e}")
            raise

    async def get_failed_operations(
            self,
            db: AsyncSession,
            start_date: Optional[datetime] = None,
            end_date: Optional[datetime] = None,
            limit: int = 100
    ) -> List[AuditLog]:
        """Get failed operations from audit logs."""
        try:
            query = (
                select(AuditLog)
                .where(AuditLog.success == False)
                .options(selectinload(AuditLog.user))
            )

            # Date filters
            if start_date:
                query = query.where(AuditLog.created_at >= start_date)
            if end_date:
                query = query.where(AuditLog.created_at <= end_date)

            query = query.order_by(AuditLog.created_at.desc()).limit(limit)

            result = await db.execute(query)
            return result.scalars().all()

        except Exception as e:
            logger.error(f"Error getting failed operations: {e}")
            raise

    async def search_logs(
            self,
            db: AsyncSession,
            search_params: Dict[str, Any],
            skip: int = 0,
            limit: int = 100
    ) -> tuple[List[AuditLog], int]:
        """Search audit logs with complex criteria."""
        try:
            query = select(AuditLog)
            count_query = select(func.count()).select_from(AuditLog)

            filters = []

            # User filter
            if 'user_ids' in search_params and search_params['user_ids']:
                filters.append(AuditLog.user_id.in_(search_params['user_ids']))

            # Action filter
            if 'actions' in search_params and search_params['actions']:
                filters.append(AuditLog.action.in_(search_params['actions']))

            # Resource type filter
            if 'resource_types' in search_params and search_params['resource_types']:
                filters.append(AuditLog.resource_type.in_(search_params['resource_types']))

            # Date range filters
            if 'date_from' in search_params and search_params['date_from']:
                filters.append(AuditLog.created_at >= search_params['date_from'])
            if 'date_to' in search_params and search_params['date_to']:
                filters.append(AuditLog.created_at <= search_params['date_to'])

            # Severity filter
            if 'severities' in search_params and search_params['severities']:
                filters.append(AuditLog.severity.in_(search_params['severities']))

            # Success/failure filter
            if 'success_only' in search_params:
                filters.append(AuditLog.success == search_params['success_only'])

            # IP address filter
            if 'ip_address' in search_params and search_params['ip_address']:
                filters.append(AuditLog.ip_address == search_params['ip_address'])

            # Text search in error messages
            if 'error_search' in search_params and search_params['error_search']:
                filters.append(AuditLog.error_message.ilike(f"%{search_params['error_search']}%"))

            # Apply filters
            if filters:
                query = query.where(and_(*filters))
                count_query = count_query.where(and_(*filters))

            # Get total count
            total_result = await db.execute(count_query)
            total_count = total_result.scalar()

            # Apply ordering and pagination
            query = (
                query
                .options(selectinload(AuditLog.user))
                .order_by(AuditLog.created_at.desc())
                .offset(skip)
                .limit(limit)
            )

            result = await db.execute(query)
            logs = result.scalars().all()

            return logs, total_count

        except Exception as e:
            logger.error(f"Error searching audit logs: {e}")
            raise

    async def get_statistics(
            self,
            db: AsyncSession,
            start_date: Optional[date] = None,
            end_date: Optional[date] = None
    ) -> Dict[str, Any]:
        """Get audit log statistics."""
        try:
            base_query = select(AuditLog)

            # Apply date filters
            if start_date:
                base_query = base_query.where(func.date(AuditLog.created_at) >= start_date)
            if end_date:
                base_query = base_query.where(func.date(AuditLog.created_at) <= end_date)

            stats = {}

            # Count by action
            for action in AuditAction:
                query = (
                    select(func.count())
                    .select_from(base_query.where(AuditLog.action == action).subquery())
                )
                result = await db.execute(query)
                stats[f'action_{action.value}'] = result.scalar()

            # Count by severity
            for severity in AuditSeverity:
                query = (
                    select(func.count())
                    .select_from(base_query.where(AuditLog.severity == severity).subquery())
                )
                result = await db.execute(query)
                stats[f'severity_{severity.value}'] = result.scalar()

            # Success/failure ratio
            success_query = (
                select(func.count())
                .select_from(base_query.where(AuditLog.success == True).subquery())
            )
            result = await db.execute(success_query)
            stats['successful_operations'] = result.scalar()

            failure_query = (
                select(func.count())
                .select_from(base_query.where(AuditLog.success == False).subquery())
            )
            result = await db.execute(failure_query)
            stats['failed_operations'] = result.scalar()

            # Unique users
            unique_users_query = (
                select(func.count(func.distinct(AuditLog.user_id)))
                .select_from(base_query.subquery())
            )
            result = await db.execute(unique_users_query)
            stats['unique_users'] = result.scalar()

            # Total logs
            total_query = select(func.count()).select_from(base_query.subquery())
            result = await db.execute(total_query)
            stats['total_logs'] = result.scalar()

            return stats

        except Exception as e:
            logger.error(f"Error getting audit statistics: {e}")
            raise

    async def cleanup_old_logs(
            self,
            db: AsyncSession,
            retention_days: int = 365
    ) -> int:
        """Clean up old audit logs based on retention policy."""
        try:
            cutoff_date = datetime.utcnow() - timedelta(days=retention_days)

            # Delete old logs (except critical severity ones)
            query = delete(AuditLog).where(
                and_(
                    AuditLog.created_at < cutoff_date,
                    AuditLog.severity != AuditSeverity.CRITICAL
                )
            )

            result = await db.execute(query)
            await db.commit()

            deleted_count = result.rowcount
            if deleted_count > 0:
                logger.info(f"Cleaned up {deleted_count} old audit logs")

            return deleted_count

        except Exception as e:
            await db.rollback()
            logger.error(f"Error cleaning up old audit logs: {e}")
            raise

    async def export_logs_for_compliance(
            self,
            db: AsyncSession,
            start_date: date,
            end_date: date,
            include_sensitive: bool = False
    ) -> List[Dict[str, Any]]:
        """Export audit logs for compliance reporting."""
        try:
            query = (
                select(AuditLog)
                .where(
                    and_(
                        func.date(AuditLog.created_at) >= start_date,
                        func.date(AuditLog.created_at) <= end_date
                    )
                )
                .options(selectinload(AuditLog.user))
                .order_by(AuditLog.created_at.asc())
            )

            result = await db.execute(query)
            logs = result.scalars().all()

            # Convert to sanitized format
            exported_logs = []
            for log in logs:
                if log.is_sensitive and not include_sensitive:
                    # Export sanitized version for sensitive logs
                    exported_logs.append(log.sanitize_for_export())
                else:
                    # Export full details for non-sensitive logs
                    log_data = {
                        "id": log.id,
                        "timestamp": log.formatted_timestamp,
                        "user_id": log.user_id,
                        "username": log.user.username if log.user else None,
                        "action": log.action.value,
                        "resource_type": log.resource_type.value,
                        "resource_id": log.resource_id,
                        "resource_name": log.resource_name,
                        "success": log.success,
                        "severity": log.severity.value,
                        "ip_address": log.ip_address,
                        "error_message": log.error_message,
                        "summary": log.summary,
                    }
                    exported_logs.append(log_data)

            logger.info(f"Exported {len(exported_logs)} audit logs for compliance")
            return exported_logs

        except Exception as e:
            logger.error(f"Error exporting logs for compliance: {e}")
            raise


# Create instance
audit_log = CRUDAuditLog(AuditLog)
