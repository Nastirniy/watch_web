from typing import Dict, Optional

from fastapi import Request

from app.backend.auth.context import AuthContext
from app.backend.models.audit import AuditLog
from app.backend.utils.text import fit_column


def client_info(request: Request, auth: Optional[AuthContext] = None) -> Dict[str, Optional[str]]:
    """Who made the request, for create_audit_log(**client_info(request, auth)).

    The client address is request.client.host: behind nginx uvicorn runs with --proxy-headers
    (Dockerfile, DEPLOYMENT.md), so it is already the real client, no X-Forwarded-For parsing here.

    user_agent and request_url come from the client and are cut to their columns (a long search URL must not fail the
    audit INSERT on MySQL)."""
    return {
        "session_id": auth.session_id if auth is not None else None,
        "ip_address": request.client.host if request.client else None,
        "user_agent": fit_column(AuditLog.user_agent, request.headers.get("user-agent")),
        "request_url": fit_column(AuditLog.request_url, str(request.url)),
    }
