"""
Base schemas for API request/response models.
Provides common response formats and pagination.
"""

from datetime import datetime
from typing import Any, Dict, Generic, List, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field

# Type variable for generic responses
T = TypeVar('T')


class BaseSchema(BaseModel):
    """Base schema with common configuration."""

    model_config = ConfigDict(
        from_attributes=True,  # Enable ORM mode
        populate_by_name=True,  # Allow field population by alias
        validate_assignment=True,  # Validate on assignment
        str_strip_whitespace=True,  # Strip whitespace from strings
        use_enum_values=True,  # Use enum values instead of enum objects
    )


class TimestampSchema(BaseSchema):
    """Schema with timestamp fields."""

    created_at: Optional[datetime] = Field(None, description="Creation timestamp")
    updated_at: Optional[datetime] = Field(None, description="Last update timestamp")


class ResponseSchema(BaseSchema, Generic[T]):
    """Generic response schema wrapper."""

    success: bool = Field(True, description="Operation success status")
    message: Optional[str] = Field(None, description="Response message")
    data: Optional[T] = Field(None, description="Response data")
    errors: Optional[Dict[str, Any]] = Field(None, description="Validation errors")


class PaginationMeta(BaseSchema):
    """Pagination metadata."""

    page: int = Field(1, ge=1, description="Current page number")
    per_page: int = Field(20, ge=1, le=100, description="Items per page")
    total: int = Field(0, ge=0, description="Total number of items")
    pages: int = Field(0, ge=0, description="Total number of pages")
    has_prev: bool = Field(False, description="Has previous page")
    has_next: bool = Field(False, description="Has next page")
    prev_page: Optional[int] = Field(None, description="Previous page number")
    next_page: Optional[int] = Field(None, description="Next page number")


class PaginatedResponse(BaseSchema, Generic[T]):
    """Paginated response schema."""

    items: List[T] = Field(default_factory=list, description="List of items")
    pagination: PaginationMeta = Field(description="Pagination metadata")
    success: bool = Field(True, description="Operation success status")
    message: Optional[str] = Field(None, description="Response message")


class ErrorDetail(BaseSchema):
    """Error detail schema."""

    type: str = Field(description="Error type")
    message: str = Field(description="Error message")
    field: Optional[str] = Field(None, description="Field that caused the error")
    code: Optional[str] = Field(None, description="Error code")


class ErrorResponse(BaseSchema):
    """Error response schema."""

    success: bool = Field(False, description="Operation success status")
    message: str = Field(description="Error message")
    errors: List[ErrorDetail] = Field(default_factory=list, description="List of errors")
    timestamp: datetime = Field(default_factory=datetime.utcnow, description="Error timestamp")
    path: Optional[str] = Field(None, description="Request path that caused the error")


class HealthResponse(BaseSchema):
    """Health check response schema."""

    status: str = Field("healthy", description="Health status")
    timestamp: datetime = Field(default_factory=datetime.utcnow, description="Check timestamp")
    version: str = Field(description="Application version")
    uptime: Optional[int] = Field(None, description="Uptime in seconds")
    database: str = Field("connected", description="Database status")
    dependencies: Dict[str, str] = Field(default_factory=dict, description="Dependencies status")


class MessageResponse(BaseSchema):
    """Simple message response schema."""

    success: bool = Field(True, description="Operation success status")
    message: str = Field(description="Response message")


class IdResponse(BaseSchema):
    """Response with ID only."""

    id: int = Field(description="Resource ID")
    success: bool = Field(True, description="Operation success status")
    message: Optional[str] = Field(None, description="Response message")


class BulkOperationResponse(BaseSchema):
    """Bulk operation response schema."""

    success: bool = Field(True, description="Operation success status")
    message: str = Field(description="Response message")
    processed: int = Field(0, ge=0, description="Number of items processed")
    succeeded: int = Field(0, ge=0, description="Number of successful operations")
    failed: int = Field(0, ge=0, description="Number of failed operations")
    errors: List[ErrorDetail] = Field(default_factory=list, description="List of errors")


class SearchFilters(BaseSchema):
    """Base search filters schema."""

    q: Optional[str] = Field(None, description="Search query")
    page: int = Field(1, ge=1, description="Page number")
    per_page: int = Field(20, ge=1, le=100, description="Items per page")
    sort_by: Optional[str] = Field(None, description="Field to sort by")
    sort_order: str = Field("desc", regex="^(asc|desc)$", description="Sort order")


class DateRangeFilter(BaseSchema):
    """Date range filter schema."""

    date_from: Optional[datetime] = Field(None, description="Start date")
    date_to: Optional[datetime] = Field(None, description="End date")


class StatusFilter(BaseSchema):
    """Status filter schema."""

    is_active: Optional[bool] = Field(None, description="Filter by active status")
    is_deleted: Optional[bool] = Field(None, description="Include deleted items")


def create_paginated_response(
        items: List[T],
        total: int,
        page: int,
        per_page: int,
        message: Optional[str] = None
) -> PaginatedResponse[T]:
    """Helper function to create paginated response."""

    pages = (total + per_page - 1) // per_page  # Ceiling division
    has_prev = page > 1
    has_next = page < pages
    prev_page = page - 1 if has_prev else None
    next_page = page + 1 if has_next else None

    pagination = PaginationMeta(
        page=page,
        per_page=per_page,
        total=total,
        pages=pages,
        has_prev=has_prev,
        has_next=has_next,
        prev_page=prev_page,
        next_page=next_page,
    )

    return PaginatedResponse(
        items=items,
        pagination=pagination,
        message=message,
    )


def create_response(
        data: Optional[T] = None,
        message: Optional[str] = None,
        success: bool = True,
        errors: Optional[Dict[str, Any]] = None
) -> ResponseSchema[T]:
    """Helper function to create response."""

    return ResponseSchema(
        success=success,
        message=message,
        data=data,
        errors=errors,
    )


def create_error_response(
        message: str,
        errors: Optional[List[ErrorDetail]] = None,
        path: Optional[str] = None
) -> ErrorResponse:
    """Helper function to create error response."""

    return ErrorResponse(
        message=message,
        errors=errors or [],
        path=path,
    )
