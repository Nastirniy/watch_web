"""
Logging middleware for request context
"""

import uuid
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from app.backend.utils.logging_config import set_request_context, clear_request_context, get_api_logger

logger = get_api_logger()


class LoggingMiddleware(BaseHTTPMiddleware):
    """Middleware to add request context to logs"""
    
    async def dispatch(self, request: Request, call_next):
        # Generate request ID
        request_id = str(uuid.uuid4())[:8]
        
        # Set request context
        set_request_context(request_id=request_id)
        
        # Log request start
        logger.info(f"Request started: {request.method} {request.url.path}")
        
        try:
            # Process request
            response = await call_next(request)
            
            # Log response
            logger.info(f"Request completed: {response.status_code}")
            
            return response
            
        except Exception as e:
            logger.error(f"Request failed: {str(e)}")
            raise
        finally:
            # Clear request context
            clear_request_context()