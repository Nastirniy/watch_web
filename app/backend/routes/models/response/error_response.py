from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field


class RequiredPermissions(BaseModel):
    """Permission requirement of a route, mirrors services.permissions.Require"""
    all: List[str] = Field(default_factory=list, description="Every permission in this list is required")
    any: List[str] = Field(default_factory=list, description="At least one permission in this list is required")
    admin: bool = Field(default=False, description="admin.true is additionally required")


class ErrorDetail(BaseModel):
    """Structured error body used for 401 (AuthErrorCode) and 403 (AccessErrorCode) responses"""
    code: str = Field(description="Machine readable error code")
    message: str = Field(description="Human readable message")
    required: Optional[RequiredPermissions] = Field(default=None, description="What the route requires (403 only)")
    missing: List[str] = Field(default_factory=list, description="Permissions the caller lacks (403 only)")


class ErrorResponse(BaseModel):
    detail: ErrorDetail


# Attach to APIRouter(responses=ACCESS_ERROR_RESPONSES) so OpenAPI documents the bodies
ACCESS_ERROR_RESPONSES: Dict[int, Dict[str, Any]] = {
    401: {"model": ErrorResponse, "description": "Not authenticated"},
    403: {"model": ErrorResponse, "description": "Not authorized"},
}
