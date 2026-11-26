"""
Centralized logging configuration for the Radio Monitoring System
"""

import sys
import logging
from pathlib import Path
from typing import Optional
from loguru import logger
from contextvars import ContextVar

from app.backend.enums.general import AppEnvironment

# Context variables for request tracking
request_id_context: ContextVar[Optional[str]] = ContextVar('request_id', default=None)
user_id_context: ContextVar[Optional[int]] = ContextVar('user_id', default=None)
session_id_context: ContextVar[Optional[str]] = ContextVar('session_id', default=None)


def _with_request_id(record) -> None:
    """loguru patcher: the X-Request-ID of the current request (request_id.py), "-" outside a request"""
    record["extra"]["request_id"] = request_id_context.get() or "-"


class LoggingConfig:
    """Centralized logging configuration"""
    
    def __init__(self):
        self.is_configured = False
        self.log_file_path: Optional[str] = None
        self.log_level: str = "INFO"
        self.environment: str = AppEnvironment.PRODUCTION.value
        self.enable_file_logging: bool = False
    
    def configure(
        self,
        log_file_path: Optional[str] = None,
        log_level: str = "INFO",
        environment: str = AppEnvironment.PRODUCTION.value,
        enable_context: bool = True
    ):
        """
        Configure logging system
        
        Args:
            log_file_path: Path to log file. If None, logs only to stdout
            log_level: Logging level (DEBUG, INFO, WARNING, ERROR)
            environment: Environment (development, production)
            enable_context: Enable request context in logs
        """
        if self.is_configured:
            return
        
        # Remove default logger
        logger.remove()
        
        # Store configuration
        self.log_file_path = log_file_path
        self.log_level = log_level.upper()
        self.environment = environment.lower()
        self.enable_file_logging = log_file_path is not None
        
        # Every record carries the X-Request-ID of the request it belongs to ("-" outside a request), the id nginx
        # writes into its access log too (request_id.py)
        logger.configure(patcher=_with_request_id)

        # Define log format based on environment
        if self.environment == AppEnvironment.DEVELOPMENT.value:
            format_string = (
                "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
                "<level>{level: <8}</level> | {extra[request_id]} | "
                "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
                "<level>{message}</level>"
            )
        else:
            # Production format - structured logging
            format_string = (
                "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {extra[request_id]} | "
                "{name}:{function}:{line} | {message}"
            )
        
        # Add stdout handler. Every sink has diagnose=False: loguru's default prints the value of every variable in
        # a traceback, request bodies with their tokens included, which never belong in a log (tests/api/test_server_logging.py)
        logger.add(
            sys.stdout,
            format=format_string,
            level=self.log_level,
            colorize=self.environment == AppEnvironment.DEVELOPMENT.value,
            diagnose=False,
        )
        
        # Add file handler if log path is provided
        if self.enable_file_logging:
            # Create log directory if it doesn't exist
            log_path = Path(log_file_path)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            
            # File format (always structured for easy parsing)
            file_format = (
                "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | {extra[request_id]} | "
                "{name}:{function}:{line} | {message}"
            )
            
            logger.add(
                log_file_path,
                format=file_format,
                level=self.log_level,
                rotation="10 MB",
                retention="30 days",
                compression="gz",
                diagnose=False,
            )
        
            # Add error file handler for ERROR and CRITICAL levels
            error_log_path = str(log_path.parent / f"{log_path.stem}_errors{log_path.suffix}")
            logger.add(
                error_log_path,
                format=file_format,
                level="ERROR",
                rotation="5 MB",
                retention="60 days",
                compression="gz",
                diagnose=False,
            )
        
        # Setup uvicorn logging integration
        self._setup_uvicorn_logging()
        
        self.is_configured = True
        logger.info(f"Logging configured - Level: {self.log_level}, File: {self.enable_file_logging}")
    
    
    def _setup_uvicorn_logging(self):
        """Setup Uvicorn logging to use Loguru"""
        # Get all uvicorn-related loggers
        uvicorn_loggers = [
            "uvicorn",
            "uvicorn.access", 
            "uvicorn.error",
            "fastapi",
        ]
        
        # Create enhanced Loguru handler for standard logging
        class UvicornInterceptHandler(logging.Handler):
            def emit(self, record):
                # Get corresponding Loguru level if it exists
                try:
                    level = logger.level(record.levelname).name
                except ValueError:
                    level = record.levelno

                # Get the proper logger name for better identification
                logger_name = record.name
                
                # Create a custom logger record for uvicorn with proper name
                message = record.getMessage()
                
                # Override the record name for better display in logs  
                original_name = record.name
                original_func = record.funcName
                original_line = record.lineno
                
                # Use loguru's patch feature to override record values for display
                logger.patch(lambda r: r.update(
                    name=original_name,
                    function=original_func or "unknown", 
                    line=original_line or 0
                )).opt(exception=record.exc_info).log(level, message)
        
        # Create handler instance
        intercept_handler = UvicornInterceptHandler()
        
        # Clear existing handlers and add our interceptor
        for logger_name in uvicorn_loggers:
            uvicorn_logger = logging.getLogger(logger_name)
            uvicorn_logger.handlers.clear()
            uvicorn_logger.addHandler(intercept_handler)
            uvicorn_logger.propagate = False  # Prevent double logging
        
        # Set logging level for uvicorn loggers to match our configuration
        for logger_name in uvicorn_loggers:
            logging.getLogger(logger_name).setLevel(
                getattr(logging, self.log_level)
            )


# Global logging configuration instance
logging_config = LoggingConfig()


def setup_logging(
    log_file_path: Optional[str] = None,
    log_level: str = "INFO",
    environment: str = AppEnvironment.PRODUCTION.value
):
    """
    Setup logging system
    
    Args:
        log_file_path: Path to log file. If None, logs only to stdout
        log_level: Logging level
        environment: Environment
    """
    logging_config.configure(
        log_file_path=log_file_path,
        log_level=log_level,
        environment=environment
    )


def get_logger(name: str = None):
    """
    Get logger instance with optional name and context
    
    Args:
        name: Logger name (module name)
        
    Returns:
        Configured logger instance with context if available
    """
    # Get context values
    request_id = request_id_context.get()
    user_id = user_id_context.get()
    session_id = session_id_context.get()
    
    # Create logger with context if available
    if request_id or user_id or session_id:
        context_info = []
        if request_id:
            context_info.append(f"req:{request_id}")
        if user_id:
            context_info.append(f"user:{user_id}")
        if session_id:
            context_info.append(f"sess:{session_id[:8]}")  # Show only first 8 chars of session
        
        context_str = " | ".join(context_info)
        if name:
            return logger.bind(name=name, context=context_str)
        return logger.bind(context=context_str)
    
    # No context available
    if name:
        return logger.bind(name=name)
    return logger


def get_contextual_logger(name: str = None):
    """
    Get logger that always includes current context in messages
    
    Args:
        name: Logger name (module name)
        
    Returns:
        Logger instance that prefixes messages with context
    """
    base_logger = get_logger(name)
    
    class ContextualLogger:
        def __init__(self, logger_instance):
            self._logger = logger_instance
            
        def _format_message(self, message: str) -> str:
            """Add context prefix to message if available"""
            request_id = request_id_context.get()
            user_id = user_id_context.get()
            session_id = session_id_context.get()
            
            if request_id or user_id or session_id:
                context_parts = []
                if request_id:
                    context_parts.append(f"req:{request_id}")
                if user_id:
                    context_parts.append(f"user:{user_id}")
                if session_id:
                    context_parts.append(f"sess:{session_id[:8]}")
                
                context_prefix = "[" + " | ".join(context_parts) + "] "
                return context_prefix + message
            return message
            
        def debug(self, message: str):
            self._logger.debug(self._format_message(message))
            
        def info(self, message: str):
            self._logger.info(self._format_message(message))
            
        def warning(self, message: str):
            self._logger.warning(self._format_message(message))
            
        def error(self, message: str):
            self._logger.error(self._format_message(message))
            
        def critical(self, message: str):
            self._logger.critical(self._format_message(message))
    
    return ContextualLogger(base_logger)


def set_request_context(request_id: Optional[str] = None, user_id: Optional[int] = None, session_id: Optional[str] = None):
    """
    Set request context for logging
    
    Args:
        request_id: Request ID
        user_id: User ID
        session_id: Session ID
    """
    if request_id:
        request_id_context.set(request_id)
    if user_id:
        user_id_context.set(user_id)
    if session_id:
        session_id_context.set(session_id)


def clear_request_context():
    """Clear request context"""
    request_id_context.set(None)
    user_id_context.set(None)
    session_id_context.set(None)


# Specialized loggers for different components
def get_auth_logger():
    """Get logger for authentication operations"""
    return get_logger("auth")


def get_database_logger():
    """Get logger for database operations"""
    return get_logger("database")


def get_security_logger():
    """Get logger for security events"""
    return get_logger("security")


def get_api_logger():
    """Get logger for API requests"""
    return get_logger("api")


def get_audit_logger():
    """Get logger for audit events"""
    return get_logger("audit")


def get_main_logger():
    """Get logger for main point"""
    return get_logger("main")

