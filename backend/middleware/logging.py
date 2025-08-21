"""
Logging middleware using loguru for comprehensive request/response logging.
Provides structured logging with performance metrics and error tracking.
"""

import time
import uuid
from typing import Callable, Optional

from fastapi import Request, Response
from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from backend.core.config.config import settings


class LoggingMiddleware(BaseHTTPMiddleware):
    """Middleware for logging HTTP requests and responses."""

    def __init__(self, app: ASGIApp, log_level: str = "INFO"):
        """
        Initialize logging middleware.

        Args:
            app: ASGI application
            log_level: Logging level for requests
        """
        super().__init__(app)
        self.log_level = log_level.upper()

        # Paths to exclude from detailed logging
        self.exclude_paths = {
            "/health",
            "/docs",
            "/redoc",
            "/openapi.json",
            "/favicon.ico",
        }

        # Sensitive headers to mask in logs
        self.sensitive_headers = {
            "authorization",
            "cookie",
            "x-api-key",
            "x-auth-token",
        }

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """
        Process request and response with logging.

        Args:
            request: HTTP request
            call_next: Next middleware/handler

        Returns:
            HTTP response
        """
        # Generate unique request ID
        request_id = str(uuid.uuid4())

        # Start timing
        start_time = time.time()

        # Extract request information
        method = request.method
        url = str(request.url)
        path = request.url.path
        query_params = dict(request.query_params) if request.query_params else None
        client_ip = self._get_client_ip(request)
        user_agent = request.headers.get("user-agent")
        content_length = request.headers.get("content-length")

        # Add request ID to request state for use in handlers
        request.state.request_id = request_id

        # Skip detailed logging for excluded paths
        should_log_details = path not in self.exclude_paths

        if should_log_details:
            # Log request
            logger.bind(
                request_id=request_id,
                method=method,
                path=path,
                client_ip=client_ip,
                user_agent=user_agent,
                content_length=content_length,
                query_params=query_params,
                headers=self._sanitize_headers(dict(request.headers))
            ).info(f"Request started: {method} {path}")

        # Process request
        response = None
        error = None

        try:
            response = await call_next(request)
        except Exception as e:
            error = e
            logger.bind(
                request_id=request_id,
                method=method,
                path=path,
                error=str(e),
                error_type=type(e).__name__
            ).error(f"Request failed with exception: {method} {path}")
            raise
        finally:
            # Calculate processing time
            process_time = time.time() - start_time

            if response is not None:
                # Extract response information
                status_code = response.status_code
                response_size = response.headers.get("content-length")

                # Add performance headers
                response.headers["X-Request-ID"] = request_id
                response.headers["X-Process-Time"] = str(round(process_time, 4))

                # Determine log level based on status code
                if status_code >= 500:
                    log_level = "error"
                elif status_code >= 400:
                    log_level = "warning"
                else:
                    log_level = "info"

                if should_log_details or status_code >= 400:
                    # Log response
                    log_data = {
                        "request_id": request_id,
                        "method": method,
                        "path": path,
                        "status_code": status_code,
                        "process_time": round(process_time, 4),
                        "client_ip": client_ip,
                        "response_size": response_size,
                    }

                    message = f"Request completed: {method} {path} - {status_code} ({process_time:.4f}s)"

                    if log_level == "error":
                        logger.bind(**log_data).error(message)
                    elif log_level == "warning":
                        logger.bind(**log_data).warning(message)
                    else:
                        logger.bind(**log_data).info(message)

                # Log slow requests
                if process_time > 5.0:  # Configurable threshold
                    logger.bind(
                        request_id=request_id,
                        method=method,
                        path=path,
                        process_time=round(process_time, 4),
                        status_code=status_code
                    ).warning(f"Slow request detected: {method} {path} took {process_time:.4f}s")

        return response

    def _get_client_ip(self, request: Request) -> str:
        """
        Extract client IP address from request.

        Args:
            request: HTTP request

        Returns:
            Client IP address
        """
        # Check X-Forwarded-For header (proxy/load balancer)
        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for:
            # Take the first IP in the chain
            return forwarded_for.split(",")[0].strip()

        # Check X-Real-IP header (nginx)
        real_ip = request.headers.get("x-real-ip")
        if real_ip:
            return real_ip

        # Fall back to client address
        client_host = getattr(request.client, "host", "unknown") if request.client else "unknown"
        return client_host

    def _sanitize_headers(self, headers: dict) -> dict:
        """
        Sanitize sensitive headers for logging.

        Args:
            headers: Request headers

        Returns:
            Sanitized headers
        """
        sanitized = {}

        for key, value in headers.items():
            key_lower = key.lower()

            if key_lower in self.sensitive_headers:
                # Mask sensitive headers
                if value:
                    sanitized[key] = f"{value[:4]}***" if len(value) > 4 else "***"
                else:
                    sanitized[key] = "***"
            else:
                sanitized[key] = value

        return sanitized


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Middleware to add request context to loguru."""

    def __init__(self, app: ASGIApp):
        """
        Initialize request context middleware.

        Args:
            app: ASGI application
        """
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """
        Add request context to logger.

        Args:
            request: HTTP request
            call_next: Next middleware/handler

        Returns:
            HTTP response
        """
        # Extract context information
        request_id = getattr(request.state, "request_id", "unknown")
        method = request.method
        path = request.url.path
        client_ip = request.client.host if request.client else "unknown"

        # Bind context to logger for this request
        with logger.contextualize(
                request_id=request_id,
                method=method,
                path=path,
                client_ip=client_ip
        ):
            response = await call_next(request)
            return response


class SecurityLoggingMiddleware(BaseHTTPMiddleware):
    """Middleware for logging security-related events."""

    def __init__(self, app: ASGIApp):
        """
        Initialize security logging middleware.

        Args:
            app: ASGI application
        """
        super().__init__(app)

        # Paths that require security logging
        self.security_paths = {
            "/api/v1/auth/login",
            "/api/v1/auth/logout",
            "/api/v1/auth/refresh",
            "/api/v1/auth/change-password",
        }

        # Admin paths that require extra security logging
        self.admin_paths = {
            "/api/v1/users",
            "/api/v1/roles",
            "/api/v1/permissions",
        }

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        """
        Log security-related requests and responses.

        Args:
            request: HTTP request
            call_next: Next middleware/handler

        Returns:
            HTTP response
        """
        path = request.url.path
        method = request.method

        # Check if this is a security-sensitive endpoint
        is_security_endpoint = any(path.startswith(sec_path) for sec_path in self.security_paths)
        is_admin_endpoint = any(path.startswith(admin_path) for admin_path in self.admin_paths)

        if not (is_security_endpoint or is_admin_endpoint):
            # Not a security-sensitive endpoint, continue normally
            return await call_next(request)

        # Extract security context
        request_id = getattr(request.state, "request_id", "unknown")
        client_ip = request.client.host if request.client else "unknown"
        user_agent = request.headers.get("user-agent", "unknown")
        auth_header = request.headers.get("authorization")

        # Determine if user is authenticated
        is_authenticated = bool(auth_header and auth_header.startswith("Bearer "))

        # Log security event
        logger.bind(
            request_id=request_id,
            event_type="security_request",
            method=method,
            path=path,
            client_ip=client_ip,
            user_agent=user_agent,
            is_authenticated=is_authenticated,
            is_admin_endpoint=is_admin_endpoint
        ).info(f"Security endpoint accessed: {method} {path}")

        # Process request
        start_time = time.time()

        try:
            response = await call_next(request)

            # Log security response
            process_time = time.time() - start_time
            status_code = response.status_code

            # Determine if this was a successful authentication
            auth_success = (
                    is_security_endpoint and
                    path.endswith("/login") and
                    status_code == 200
            )

            # Determine if this was a failed authentication
            auth_failure = (
                    is_security_endpoint and
                    path.endswith("/login") and
                    status_code in [401, 403]
            )

            log_level = "info"
            if auth_failure or status_code >= 400:
                log_level = "warning"
            if status_code >= 500:
                log_level = "error"

            log_data = {
                "request_id": request_id,
                "event_type": "security_response",
                "method": method,
                "path": path,
                "status_code": status_code,
                "process_time": round(process_time, 4),
                "client_ip": client_ip,
                "auth_success": auth_success,
                "auth_failure": auth_failure,
            }

            message = f"Security endpoint response: {method} {path} - {status_code}"

            if log_level == "error":
                logger.bind(**log_data).error(message)
            elif log_level == "warning":
                logger.bind(**log_data).warning(message)
            else:
                logger.bind(**log_data).info(message)

            return response

        except Exception as e:
            # Log security exception
            logger.bind(
                request_id=request_id,
                event_type="security_error",
                method=method,
                path=path,
                client_ip=client_ip,
                error=str(e),
                error_type=type(e).__name__
            ).error(f"Security endpoint error: {method} {path}")

            raise


def setup_logging():
    """Configure loguru logging based on application settings."""

    # Remove default handler
    logger.remove()

    # Console logging with colors
    logger.add(
        sink=lambda msg: print(msg, end=""),
        level=settings.LOG_LEVEL,
        format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
               "<level>{level: <8}</level> | "
               "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
               "<level>{message}</level>",
        colorize=True,
        backtrace=True,
        diagnose=True,
    )

    # File logging if configured
    if settings.LOG_FILE:
        logger.add(
            sink=settings.LOG_FILE,
            level=settings.LOG_LEVEL,
            format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {name}:{function}:{line} | {extra} | {message}",
            rotation=settings.LOG_ROTATION,
            retention=settings.LOG_RETENTION,
            compression="gz",
            backtrace=True,
            diagnose=True,
            serialize=True,  # JSON format for structured logging
        )

    # Configure structured logging
    logger.configure(
        handlers=[
            {
                "sink": lambda msg: print(msg, end=""),
                "level": settings.LOG_LEVEL,
                "format": "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
                          "<level>{level: <8}</level> | "
                          "<cyan>{extra[request_id]}</cyan> | "
                          "<cyan>{name}</cyan>:<cyan>{function}</cyan> | "
                          "<level>{message}</level>",
                "filter": lambda record: "request_id" in record["extra"],
            }
        ]
    )

    logger.info(f"Logging configured - Level: {settings.LOG_LEVEL}")


# Initialize logging when module is imported
setup_logging()
