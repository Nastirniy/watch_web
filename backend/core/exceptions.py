"""
Custom exception handling with human-readable error messages.
Provides structured error responses and exception handlers for FastAPI.
"""

import traceback
from typing import Any, Dict, List, Optional, Union

from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from backend.api.schemas import ErrorDetail, ErrorResponse


class CustomHTTPException(HTTPException):
    """Custom HTTP exception with additional metadata."""

    def __init__(
        self,
        status_code: int,
        detail: str,
        headers: Optional[Dict[str, Any]] = None,
        error_code: Optional[str] = None,
        field: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None
    ):
        """
        Initialize custom HTTP exception.

        Args:
            status_code: HTTP status code
            detail: Error message
            headers: HTTP headers
            error_code: Application-specific error code
            field: Field that caused the error
            context: Additional error context
        """
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.error_code = error_code
        self.field = field
        self.context = context or {}


class ValidationException(Exception):
    """Exception for validation errors."""

    def __init__(
        self,
        message: str,
        errors: Optional[List[ErrorDetail]] = None,
        field: Optional[str] = None
    ):
        """
        Initialize validation exception.

        Args:
            message: Error message
            errors: List of validation errors
            field: Field that caused the error
        """
        super().__init__(message)
        self.message = message
        self.errors = errors or []
        self.field = field


class AuthenticationException(CustomHTTPException):
    """Exception for authentication errors."""

    def __init__(
        self,
        detail: str = "Authentication failed",
        error_code: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None
    ):
        super().__init__(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=detail,
            headers={"WWW-Authenticate": "Bearer"},
            error_code=error_code or "AUTH_FAILED",
            context=context
        )


class AuthorizationException(CustomHTTPException):
    """Exception for authorization errors."""

    def __init__(
        self,
        detail: str = "Insufficient permissions",
        error_code: Optional[str] = None,
        required_permission: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None
    ):
        ctx = context or {}
        if required_permission:
            ctx["required_permission"] = required_permission

        super().__init__(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=detail,
            error_code=error_code or "AUTH_INSUFFICIENT_PERMISSIONS",
            context=ctx
        )


class ResourceNotFoundException(CustomHTTPException):
    """Exception for resource not found errors."""

    def __init__(
        self,
        resource: str,
        identifier: Union[str, int],
        error_code: Optional[str] = None
    ):
        super().__init__(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{resource} with identifier '{identifier}' not found",
            error_code=error_code or "RESOURCE_NOT_FOUND",
            context={"resource": resource, "identifier": str(identifier)}
        )


class ResourceConflictException(CustomHTTPException):
    """Exception for resource conflict errors."""

    def __init__(
        self,
        detail: str,
        error_code: Optional[str] = None,
        field: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None
    ):
        super().__init__(
            status_code=status.HTTP_409_CONFLICT,
            detail=detail,
            error_code=error_code or "RESOURCE_CONFLICT",
            field=field,
            context=context
        )


class BusinessLogicException(CustomHTTPException):
    """Exception for business logic violations."""

    def __init__(
        self,
        detail: str,
        error_code: Optional[str] = None,
        field: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None
    ):
        super().__init__(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=detail,
            error_code=error_code or "BUSINESS_LOGIC_ERROR",
            field=field,
            context=context
        )


class ExternalServiceException(CustomHTTPException):
    """Exception for external service errors."""

    def __init__(
        self,
        service: str,
        detail: str,
        error_code: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None
    ):
        ctx = context or {}
        ctx["service"] = service

        super().__init__(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"External service '{service}' error: {detail}",
            error_code=error_code or "EXTERNAL_SERVICE_ERROR",
            context=ctx
        )


class RateLimitException(CustomHTTPException):
    """Exception for rate limiting errors."""

    def __init__(
        self,
        detail: str = "Rate limit exceeded",
        retry_after: Optional[int] = None,
        error_code: Optional[str] = None
    ):
        headers = {}
        if retry_after:
            headers["Retry-After"] = str(retry_after)

        super().__init__(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=detail,
            headers=headers,
            error_code=error_code or "RATE_LIMIT_EXCEEDED",
            context={"retry_after": retry_after} if retry_after else None
        )


class FileProcessingException(CustomHTTPException):
    """Exception for file processing errors."""

    def __init__(
        self,
        detail: str,
        filename: Optional[str] = None,
        error_code: Optional[str] = None,
        context: Optional[Dict[str, Any]] = None
    ):
        ctx = context or {}
        if filename:
            ctx["filename"] = filename

        super().__init__(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=detail,
            error_code=error_code or "FILE_PROCESSING_ERROR",
            context=ctx
        )


def create_error_detail(
    error_type: str,
    message: str,
    field: Optional[str] = None,
    code: Optional[str] = None
) -> ErrorDetail:
    """
    Create an error detail object.

    Args:
        error_type: Type of error
        message: Error message
        field: Field that caused the error
        code: Error code

    Returns:
        ErrorDetail object
    """
    return ErrorDetail(
        type=error_type,
        message=message,
        field=field,
        code=code
    )


def format_validation_errors(exc: ValidationError) -> List[ErrorDetail]:
    """
    Format Pydantic validation errors into ErrorDetail objects.

    Args:
        exc: Pydantic ValidationError

    Returns:
        List of formatted error details
    """
    errors = []

    for error in exc.errors():
        field_path = ".".join(str(loc) for loc in error["loc"]) if error["loc"] else None
        error_type = error["type"]
        message = error["msg"]

        # Create human-readable error messages
        if error_type == "missing":
            message = f"Field is required"
        elif error_type == "string_too_short":
            min_length = error.get("ctx", {}).get("limit_value", "minimum")
            message = f"Field must be at least {min_length} characters long"
        elif error_type == "string_too_long":
            max_length = error.get("ctx", {}).get("limit_value", "maximum")
            message = f"Field must be no more than {max_length} characters long"
        elif error_type == "value_error":
            message = str(error.get("ctx", {}).get("given", message))
        elif error_type.startswith("greater_than"):
            limit = error.get("ctx", {}).get("limit_value", "minimum")
            message = f"Value must be greater than {limit}"
        elif error_type.startswith("less_than"):
            limit = error.get("ctx", {}).get("limit_value", "maximum")
            message = f"Value must be less than {limit}"

        errors.append(create_error_detail(
            error_type=error_type,
            message=message,
            field=field_path,
            code=error_type.upper()
        ))

    return errors


def format_database_error(exc: SQLAlchemyError) -> ErrorDetail:
    """
    Format database errors into human-readable messages.

    Args:
        exc: SQLAlchemy exception

    Returns:
        Formatted error detail
    """
    if isinstance(exc, IntegrityError):
        # Handle common integrity constraint violations
        error_msg = str(exc.orig) if hasattr(exc, 'orig') else str(exc)

        if "UNIQUE constraint failed" in error_msg or "Duplicate entry" in error_msg:
            if "username" in error_msg:
                message = "Username already exists"
                field = "username"
            elif "email" in error_msg:
                message = "Email address already exists"
                field = "email"
            elif "code" in error_msg:
                message = "Code already exists"
                field = "code"
            else:
                message = "A record with this value already exists"
                field = None

            return create_error_detail(
                error_type="integrity_error",
                message=message,
                field=field,
                code="DUPLICATE_VALUE"
            )

        elif "FOREIGN KEY constraint failed" in error_msg:
            return create_error_detail(
                error_type="integrity_error",
                message="Referenced record does not exist",
                code="FOREIGN_KEY_VIOLATION"
            )

        elif "NOT NULL constraint failed" in error_msg:
            # Extract field name from error message
            field = None
            if "." in error_msg:
                parts = error_msg.split(".")
                if len(parts) > 1:
                    field = parts[-1].strip()

            return create_error_detail(
                error_type="integrity_error",
                message="Required field cannot be empty",
                field=field,
                code="NOT_NULL_VIOLATION"
            )

    # Generic database error
    return create_error_detail(
        error_type="database_error",
        message="A database error occurred",
        code="DATABASE_ERROR"
    )


async def handle_custom_http_exception(request: Request, exc: CustomHTTPException) -> JSONResponse:
    """
    Handle custom HTTP exceptions.

    Args:
        request: FastAPI request object
        exc: Custom HTTP exception

    Returns:
        JSON error response
    """
    logger.warning(
        f"HTTP Exception: {exc.status_code} - {exc.detail} "
        f"[Path: {request.url.path}] [Code: {exc.error_code}]"
    )

    error_detail = create_error_detail(
        error_type="http_error",
        message=exc.detail,
        field=exc.field,
        code=exc.error_code
    )

    error_response = ErrorResponse(
        message=exc.detail,
        errors=[error_detail],
        path=str(request.url.path)
    )

    return JSONResponse(
        status_code=exc.status_code,
        content=error_response.model_dump(),
        headers=exc.headers
    )


async def handle_validation_exception(request: Request, exc: ValidationException) -> JSONResponse:
    """
    Handle validation exceptions.

    Args:
        request: FastAPI request object
        exc: Validation exception

    Returns:
        JSON error response
    """
    logger.warning(f"Validation Error: {exc.message} [Path: {request.url.path}]")

    errors = exc.errors or [create_error_detail(
        error_type="validation_error",
        message=exc.message,
        field=exc.field,
        code="VALIDATION_ERROR"
    )]

    error_response = ErrorResponse(
        message=exc.message,
        errors=errors,
        path=str(request.url.path)
    )

    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=error_response.model_dump()
    )


async def handle_generic_exception(request: Request, exc: Exception) -> JSONResponse:
    """
    Handle generic exceptions with full traceback logging.

    Args:
        request: FastAPI request object
        exc: Generic exception

    Returns:
        JSON error response
    """
    # Log full traceback for debugging
    logger.error(
        f"Unhandled Exception: {type(exc).__name__}: {str(exc)} "
        f"[Path: {request.url.path}]\n{traceback.format_exc()}"
    )

    # Handle specific exception types
    if isinstance(exc, ValidationError):
        errors = format_validation_errors(exc)
        error_response = ErrorResponse(
            message="Validation failed",
            errors=errors,
            path=str(request.url.path)
        )
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content=error_response.model_dump()
        )

    elif isinstance(exc, SQLAlchemyError):
        error_detail = format_database_error(exc)
        error_response = ErrorResponse(
            message="Database operation failed",
            errors=[error_detail],
            path=str(request.url.path)
        )
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=error_response.model_dump()
        )

    elif isinstance(exc, HTTPException):
        error_detail = create_error_detail(
            error_type="http_error",
            message=exc.detail,
            code="HTTP_ERROR"
        )
        error_response = ErrorResponse(
            message=exc.detail,
            errors=[error_detail],
            path=str(request.url.path)
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=error_response.model_dump()
        )

    # Generic error response (hide internal details in production)
    from app.config import settings

    if settings.DEBUG:
        message = f"{type(exc).__name__}: {str(exc)}"
        error_detail = create_error_detail(
            error_type="internal_error",
            message=str(exc),
            code="INTERNAL_ERROR"
        )
    else:
        message = "An internal server error occurred"
        error_detail = create_error_detail(
            error_type="internal_error",
            message="Internal server error",
            code="INTERNAL_ERROR"
        )

    error_response = ErrorResponse(
        message=message,
        errors=[error_detail],
        path=str(request.url.path)
    )

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=error_response.model_dump()
    )
