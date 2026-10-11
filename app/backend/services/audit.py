"""The audit log: one AuditLog row per audited action, added to the session of the change."""
from typing import Optional, Dict, Any
from pydantic_core import to_jsonable_python

from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.models.audit import AuditLog


def serialize_for_audit(obj: Any) -> Any:
    """
    Recursively convert non-serializable objects to JSON-compatible ones using Pydantic's logic.
    Handles Decimal, datetime, UUID, Enums, etc.
    """
    if obj is None:
        return None
    return to_jsonable_python(obj)


async def create_audit_log(
    session: AsyncSession,
    action: str,
    resource_type: str,
    success: bool = True,
    user_id: Optional[int] = None,
    session_id: Optional[str] = None,
    resource_id: Optional[int] = None,
    old_values: Optional[Dict] = None,
    new_values: Optional[Dict] = None,
    details: Optional[Dict] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    request_url: Optional[str] = None,
    error_message: Optional[str] = None
) -> AuditLog:
    """
    Create and add audit log entry to session

    Args:
        session: Database session
        action: Action being audited (login, logout, create, update, delete, etc.)
        resource_type: Type of resource (user, observation, post, etc.)
        success: Whether the action succeeded
        user_id: Optional user ID
        session_id: Optional session ID
        resource_id: Optional resource ID
        old_values: Optional dict of values before the action
        new_values: Optional dict of values after the action
        details: Optional additional human-friendly details dict
        ip_address: Optional IP address
        user_agent: Optional user agent string
        request_url: Optional request URL
        error_message: Optional error message if success is False

    Returns:
        Created AuditLog instance (already added to session)
    """
    audit = AuditLog(
        user_id=user_id,
        session_id=session_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        old_values=serialize_for_audit(old_values),
        new_values=serialize_for_audit(new_values),
        details=serialize_for_audit(details),
        ip_address=ip_address,
        user_agent=user_agent,
        request_url=request_url,
        success=success,
        error_message=error_message
    )
    session.add(audit)
    return audit
