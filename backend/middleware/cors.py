"""
CORS middleware for handling cross-origin requests.
Provides flexible CORS configuration for different environments.
"""

from typing import List, Optional, Set, Union

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from backend.core.config.config import settings


class CORSMiddleware(BaseHTTPMiddleware):
    """Custom CORS middleware with enhanced configuration options."""

    def __init__(
            self,
            app: ASGIApp,
            allow_origins: Union[List[str], str] = None,
            allow_methods: List[str] = None,
            allow_headers: List[str] = None,
            allow_credentials: bool = False,
            expose_headers: List[str] = None,
            max_age: int = 600,
            allow_origin_regex: Optional[str] = None,
    ):
        """
        Initialize CORS middleware.

        Args:
            app: ASGI application
            allow_origins: Allowed origins (list or "*")
            allow_methods: Allowed HTTP methods
            allow_headers: Allowed headers
            allow_credentials: Allow credentials
            expose_headers: Headers to expose to client
            max_age: Preflight cache max age
            allow_origin_regex: Regex pattern for allowed origins
        """
        super().__init__(app)

        # Configure allowed origins
        if allow_origins is None:
            self.allow_origins = settings.BACKEND_CORS_ORIGINS
        elif isinstance(allow_origins, str):
            self.allow_origins = [allow_origins] if allow_origins != "*" else ["*"]
        else:
            self.allow_origins = list(allow_origins)

        # Configure allowed methods
        if allow_methods is None:
            self.allow_methods = [
                "GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"
            ]
        else:
            self.allow_methods = list(allow_methods)

        # Configure allowed headers
        if allow_headers is None:
            self.allow_headers = [
                "Accept",
                "Accept-Language",
                "Content-Language",
                "Content-Type",
                "Authorization",
                "X-Requested-With",
                "X-Request-ID",
                "X-API-Key",
                "Cache-Control",
                "Pragma",
            ]
        else:
            self.allow_headers = list(allow_headers)

        # Configure exposed headers
        if expose_headers is None:
            self.expose_headers = [
                "X-Request-ID",
                "X-Process-Time",
                "X-Rate-Limit-Remaining",
                "X-Rate-Limit-Reset",
                "Content-Disposition",
            ]
        else:
            self.expose_headers = list(expose_headers)

        self.allow_credentials = allow_credentials
        self.max_age = max_age
        self.allow_origin_regex = allow_origin_regex

        # Precompile values for performance
        self.allow_all_origins = "*" in self.allow_origins
        self.allow_methods_str = ", ".join(self.allow_methods)
        self.allow_headers_str = ", ".join(self.allow_headers)
        self.expose_headers_str = ", ".join(self.expose_headers)

        # Convert origins to set for faster lookup
        self.allowed_origins_set: Set[str] = set(self.allow_origins)

    async def dispatch(self, request: Request, call_next) -> Response:
        """
        Process CORS headers for requests.

        Args:
            request: HTTP request
            call_next: Next middleware/handler

        Returns:
            HTTP response with CORS headers
        """
        origin = request.headers.get("origin")

        # Handle preflight requests
        if request.method == "OPTIONS":
            return self._handle_preflight_request(request, origin)

        # Process normal request
        response = await call_next(request)

        # Add CORS headers to response
        self._add_cors_headers(response, origin)

        return response

    def _handle_preflight_request(self, request: Request, origin: Optional[str]) -> Response:
        """
        Handle CORS preflight (OPTIONS) requests.

        Args:
            request: HTTP request
            origin: Request origin

        Returns:
            Preflight response
        """
        # Check if origin is allowed
        if not self._is_origin_allowed(origin):
            return Response(status_code=400, content="Origin not allowed")

        # Check requested method
        requested_method = request.headers.get("access-control-request-method")
        if requested_method and requested_method not in self.allow_methods:
            return Response(status_code=400, content="Method not allowed")

        # Check requested headers
        requested_headers = request.headers.get("access-control-request-headers")
        if requested_headers:
            requested_headers_list = [
                header.strip().lower() for header in requested_headers.split(",")
            ]
            allowed_headers_lower = [header.lower() for header in self.allow_headers]

            for header in requested_headers_list:
                if header not in allowed_headers_lower:
                    return Response(status_code=400, content=f"Header '{header}' not allowed")

        # Create preflight response
        headers = {}

        # Add origin header
        if origin:
            headers["Access-Control-Allow-Origin"] = origin

        # Add credentials header
        if self.allow_credentials:
            headers["Access-Control-Allow-Credentials"] = "true"

        # Add allowed methods
        headers["Access-Control-Allow-Methods"] = self.allow_methods_str

        # Add allowed headers
        headers["Access-Control-Allow-Headers"] = self.allow_headers_str

        # Add max age
        headers["Access-Control-Max-Age"] = str(self.max_age)

        # Add vary header for caching
        headers["Vary"] = "Origin, Access-Control-Request-Method, Access-Control-Request-Headers"

        return Response(status_code=200, headers=headers)

    def _add_cors_headers(self, response: Response, origin: Optional[str]) -> None:
        """
        Add CORS headers to response.

        Args:
            response: HTTP response
            origin: Request origin
        """
        # Check if origin is allowed
        if not self._is_origin_allowed(origin):
            return

        # Add origin header
        if self.allow_all_origins:
            response.headers["Access-Control-Allow-Origin"] = "*"
        elif origin:
            response.headers["Access-Control-Allow-Origin"] = origin
            # Add vary header when not using wildcard
            response.headers["Vary"] = "Origin"

        # Add credentials header
        if self.allow_credentials and not self.allow_all_origins:
            response.headers["Access-Control-Allow-Credentials"] = "true"

        # Add exposed headers
        if self.expose_headers:
            response.headers["Access-Control-Expose-Headers"] = self.expose_headers_str

    def _is_origin_allowed(self, origin: Optional[str]) -> bool:
        """
        Check if origin is allowed.

        Args:
            origin: Request origin

        Returns:
            True if origin is allowed
        """
        if not origin:
            return True  # Same-origin requests don't have origin header

        if self.allow_all_origins:
            return True

        # Check exact match
        if origin in self.allowed_origins_set:
            return True

        # Check regex pattern if configured
        if self.allow_origin_regex:
            import re
            if re.match(self.allow_origin_regex, origin):
                return True

        return False


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Middleware to add security headers to responses."""

    def __init__(
            self,
            app: ASGIApp,
            enable_hsts: bool = True,
            enable_content_type_options: bool = True,
            enable_frame_options: bool = True,
            enable_xss_protection: bool = True,
            enable_content_security_policy: bool = False,
            custom_headers: Optional[dict] = None,
    ):
        """
        Initialize security headers middleware.

        Args:
            app: ASGI application
            enable_hsts: Enable HTTP Strict Transport Security
            enable_content_type_options: Enable X-Content-Type-Options
            enable_frame_options: Enable X-Frame-Options
            enable_xss_protection: Enable X-XSS-Protection
            enable_content_security_policy: Enable Content-Security-Policy
            custom_headers: Additional custom headers
        """
        super().__init__(app)

        self.security_headers = {}

        # HTTP Strict Transport Security
        if enable_hsts and not settings.DEBUG:
            self.security_headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )

        # Prevent MIME type sniffing
        if enable_content_type_options:
            self.security_headers["X-Content-Type-Options"] = "nosniff"

        # Prevent clickjacking
        if enable_frame_options:
            self.security_headers["X-Frame-Options"] = "DENY"

        # XSS protection
        if enable_xss_protection:
            self.security_headers["X-XSS-Protection"] = "1; mode=block"

        # Content Security Policy (basic policy)
        if enable_content_security_policy:
            self.security_headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data: https:; "
                "font-src 'self'; "
                "connect-src 'self'; "
                "frame-ancestors 'none'"
            )

        # Referrer policy
        self.security_headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

        # Permissions policy
        self.security_headers["Permissions-Policy"] = (
            "geolocation=(), microphone=(), camera=()"
        )

        # Add custom headers
        if custom_headers:
            self.security_headers.update(custom_headers)

    async def dispatch(self, request: Request, call_next) -> Response:
        """
        Add security headers to response.

        Args:
            request: HTTP request
            call_next: Next middleware/handler

        Returns:
            HTTP response with security headers
        """
        response = await call_next(request)

        # Add security headers
        for header_name, header_value in self.security_headers.items():
            response.headers[header_name] = header_value

        return response


class CompressionMiddleware(BaseHTTPMiddleware):
    """Simple compression middleware for text responses."""

    def __init__(
            self,
            app: ASGIApp,
            minimum_size: int = 1024,
            compress_level: int = 6,
    ):
        """
        Initialize compression middleware.

        Args:
            app: ASGI application
            minimum_size: Minimum response size to compress
            compress_level: Compression level (1-9)
        """
        super().__init__(app)
        self.minimum_size = minimum_size
        self.compress_level = compress_level

        # Content types to compress
        self.compressible_types = {
            "application/json",
            "application/javascript",
            "application/xml",
            "text/css",
            "text/html",
            "text/javascript",
            "text/plain",
            "text/xml",
        }

    async def dispatch(self, request: Request, call_next) -> Response:
        """
        Compress response if applicable.

        Args:
            request: HTTP request
            call_next: Next middleware/handler

        Returns:
            Compressed response if applicable
        """
        # Check if client accepts gzip encoding
        accept_encoding = request.headers.get("accept-encoding", "")
        if "gzip" not in accept_encoding.lower():
            return await call_next(request)

        response = await call_next(request)

        # Check if response should be compressed
        if not self._should_compress(response):
            return response

        # Compress response body
        try:
            import gzip

            # Get response body
            body = b""
            async for chunk in response.body_iterator:
                body += chunk

            # Check minimum size
            if len(body) < self.minimum_size:
                # Recreate response with original body
                return Response(
                    content=body,
                    status_code=response.status_code,
                    headers=dict(response.headers),
                    media_type=response.media_type,
                )

            # Compress body
            compressed_body = gzip.compress(body, compresslevel=self.compress_level)

            # Update headers
            headers = dict(response.headers)
            headers["Content-Encoding"] = "gzip"
            headers["Content-Length"] = str(len(compressed_body))

            # Create compressed response
            return Response(
                content=compressed_body,
                status_code=response.status_code,
                headers=headers,
                media_type=response.media_type,
            )

        except Exception as e:
            # If compression fails, return original response
            from loguru import logger
            logger.warning(f"Compression failed: {str(e)}")
            return response

    def _should_compress(self, response: Response) -> bool:
        """
        Check if response should be compressed.

        Args:
            response: HTTP response

        Returns:
            True if response should be compressed
        """
        # Don't compress if already encoded
        if "content-encoding" in response.headers:
            return False

        # Check content type
        content_type = response.headers.get("content-type", "")
        main_type = content_type.split(";")[0].strip().lower()

        return main_type in self.compressible_types
