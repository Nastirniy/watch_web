from typing import Optional, Dict

from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.models.audit import AuditLog


async def create_audit_log(
    session: AsyncSession,
    action: str,
    resource_type: str,
    success: bool,
    user_id: Optional[int] = None,
    session_id: Optional[str] = None,
    resource_id: Optional[int] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    details: Optional[Dict] = None
) -> AuditLog:
    """
    Create and add audit log entry to session

    Args:
        session: Database session
        action: Action being audited (login, logout, pki_auth_attempt, etc.)
        resource_type: Type of resource (user, session, auth, etc.)
        success: Whether the action succeeded
        user_id: Optional user ID
        session_id: Optional session ID
        resource_id: Optional resource ID
        ip_address: Optional IP address
        user_agent: Optional user agent string
        details: Optional additional details dict

    Returns:
        Created AuditLog instance (already added to session)
    """
    audit = AuditLog(
        user_id=user_id,
        session_id=session_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        ip_address=ip_address,
        user_agent=user_agent,
        success=success,
        details=details
    )
    session.add(audit)
    return audit


def truncate_for_logging(value: str, length: int = 16) -> str:
    """
    Truncate sensitive value for logging purposes

    Args:
        value: Value to truncate
        length: Number of characters to keep

    Returns:
        Truncated string with ellipsis
    """
    if not value:
        return ""
    return f"{value[:length]}..." if len(value) > length else value