"""
Audit logging models for tracking system activities and changes.
Provides comprehensive audit trail for security and compliance.
"""

from datetime import datetime
from enum import Enum
from typing import Dict, Any, Optional, TYPE_CHECKING

from sqlmodel import Field, Relationship, JSON

from backend.models.base import BaseModel

if TYPE_CHECKING:
    from backend.models.user import User


class AuditAction(str, Enum):
    """Audit action type enumeration."""
    # Authentication actions
    LOGIN = "login"
    LOGOUT = "logout"
    LOGIN_FAILED = "login_failed"
    PASSWORD_CHANGE = "password_change"

    # CRUD operations
    CREATE = "create"
    READ = "read"
    UPDATE = "update"
    DELETE = "delete"
    SOFT_DELETE = "soft_delete"
    RESTORE = "restore"

    # Bulk operations
    BULK_CREATE = "bulk_create"
    BULK_UPDATE = "bulk_update"
    BULK_DELETE = "bulk_delete"

    # Permission operations
    PERMISSION_GRANT = "permission_grant"
    PERMISSION_REVOKE = "permission_revoke"
    ROLE_ASSIGN = "role_assign"
    ROLE_REMOVE = "role_remove"

    # File operations
    FILE_UPLOAD = "file_upload"
    FILE_DOWNLOAD = "file_download"
    FILE_DELETE = "file_delete"

    # Export operations
    EXPORT_CREATE = "export_create"
    EXPORT_DOWNLOAD = "export_download"

    # System operations
    BACKUP_CREATE = "backup_create"
    SYSTEM_CONFIG = "system_config"
    MAINTENANCE = "maintenance"


class ResourceType(str, Enum):
    """Resource type enumeration for audit logging."""
    USER = "user"
    ROLE = "role"
    PERMISSION = "permission"
    POST = "post"
    SIGNAL_TYPE = "signal_type"
    OBSERVATION = "observation"
    MEDIA_FILE = "media_file"
    EXPORT_JOB = "export_job"
    SESSION = "session"
    SYSTEM = "system"
    CATEGORY = "category"
    MODULATION_TYPE = "modulation_type"


class AuditSeverity(str, Enum):
    """Audit event severity levels."""
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class AuditLog(BaseModel, table=True):
    """Comprehensive audit log for all system activities."""

    __tablename__ = "audit_logs"

    id: Optional[int] = Field(default=None, primary_key=True)

    # Actor information
    user_id: Optional[int] = Field(default=None, foreign_key="users.id", index=True, description="Acting user ID")
    session_id: Optional[str] = Field(default=None, max_length=128, index=True, description="User session ID")

    # Action details
    action: AuditAction = Field(index=True, description="Action performed")
    resource_type: ResourceType = Field(index=True, description="Type of resource affected")
    resource_id: Optional[int] = Field(default=None, index=True, description="ID of affected resource")
    resource_name: Optional[str] = Field(default=None, description="Name/identifier of affected resource")

    # Change tracking
    old_values: Optional[Dict[str, Any]] = Field(default=None, sa_column=JSON, description="Previous values (for updates/deletes)")
    new_values: Optional[Dict[str, Any]] = Field(default=None, sa_column=JSON, description="New values (for creates/updates)")
    changed_fields: Optional[list[str]] = Field(default=None, sa_column=JSON, description="List of changed field names")

    # Request context
    ip_address: Optional[str] = Field(default=None, max_length=45, description="Client IP address")
    user_agent: Optional[str] = Field(default=None, description="Client user agent")
    request_url: Optional[str] = Field(default=None, max_length=500, description="Request URL")
    request_method: Optional[str] = Field(default=None, max_length=10, description="HTTP method")

    # Operation result
    success: bool = Field(default=True, index=True, description="Operation success status")
    error_message: Optional[str] = Field(default=None, description="Error message if operation failed")
    error_code: Optional[str] = Field(default=None, description="Error code if applicable")

    # Additional metadata
    severity: AuditSeverity = Field(default=AuditSeverity.LOW, description="Event severity level")
    tags: Optional[list[str]] = Field(default=None, sa_column=JSON, description="Custom tags for categorization")
    additional_data: Optional[Dict[str, Any]] = Field(default=None, sa_column=JSON, description="Additional context data")

    # Performance metrics
    response_time_ms: Optional[int] = Field(default=None, description="Response time in milliseconds")
    records_affected: Optional[int] = Field(default=None, description="Number of records affected")

    # Compliance and retention
    retention_period_days: Optional[int] = Field(default=None, description="Data retention period")
    compliance_category: Optional[str] = Field(default=None, description="Compliance category (GDPR, SOX, etc.)")

    # Relationships
    user: Optional["User"] = Relationship(back_populates="audit_logs")

    @classmethod
    def create_log(
        cls,
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
    ) -> "AuditLog":
        """Factory method to create audit log entries."""

        # Determine changed fields for update operations
        changed_fields = None
        if action == AuditAction.UPDATE and old_values and new_values:
            changed_fields = [
                field for field in new_values.keys()
                if field in old_values and old_values[field] != new_values[field]
            ]

        return cls(
            user_id=user_id,
            session_id=session_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            resource_name=resource_name,
            old_values=old_values,
            new_values=new_values,
            changed_fields=changed_fields,
            ip_address=ip_address,
            user_agent=user_agent,
            request_url=request_url,
            request_method=request_method,
            success=success,
            error_message=error_message,
            severity=severity,
            **kwargs
        )

    @property
    def is_sensitive(self) -> bool:
        """Check if log entry contains sensitive information."""
        sensitive_actions = [
            AuditAction.LOGIN,
            AuditAction.LOGIN_FAILED,
            AuditAction.PASSWORD_CHANGE,
            AuditAction.PERMISSION_GRANT,
            AuditAction.PERMISSION_REVOKE,
        ]
        return self.action in sensitive_actions or self.severity in [AuditSeverity.HIGH, AuditSeverity.CRITICAL]

    @property
    def formatted_timestamp(self) -> str:
        """Get formatted timestamp for display."""
        if self.created_at:
            return self.created_at.strftime("%Y-%m-%d %H:%M:%S UTC")
        return "Unknown"

    @property
    def summary(self) -> str:
        """Get human-readable summary of the audit event."""
        parts = []

        if self.user_id:
            parts.append(f"User {self.user_id}")
        else:
            parts.append("System")

        parts.append(self.action.value)
        parts.append(self.resource_type.value)

        if self.resource_name:
            parts.append(f"'{self.resource_name}'")
        elif self.resource_id:
            parts.append(f"ID {self.resource_id}")

        if not self.success and self.error_message:
            parts.append(f"(Failed: {self.error_message})")

        return " ".join(parts)

    def sanitize_for_export(self) -> Dict[str, Any]:
        """Get sanitized version of audit log for export."""
        data = {
            "id": self.id,
            "timestamp": self.formatted_timestamp,
            "action": self.action.value,
            "resource_type": self.resource_type.value,
            "success": self.success,
            "severity": self.severity.value,
        }

        # Include non-sensitive fields
        if self.resource_id:
            data["resource_id"] = self.resource_id
        if self.resource_name:
            data["resource_name"] = self.resource_name
        if self.error_message and not self.is_sensitive:
            data["error_message"] = self.error_message

        return data
