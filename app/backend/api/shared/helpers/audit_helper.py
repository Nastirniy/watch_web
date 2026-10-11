from typing import Dict, Optional

from fastapi import Request

from app.backend.auth.context import AuthContext


def client_info(request: Request, auth: Optional[AuthContext] = None) -> Dict[str, Optional[str]]:
    """Who made the request, for create_audit_log(**client_info(request, auth)).

    The client address is request.client.host: behind nginx uvicorn runs with --proxy-headers
    (Dockerfile, DEPLOYMENT.md), so it is already the real client, no X-Forwarded-For parsing here."""
    return {
        "session_id": auth.session_id if auth is not None else None,
        "ip_address": request.client.host if request.client else None,
        "user_agent": request.headers.get("user-agent"),
        "request_url": str(request.url),
    }
