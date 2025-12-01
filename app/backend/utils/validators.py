"""
Comprehensive input validation and sanitization utilities
Following OWASP security guidelines and industry standards
"""

import re
import html
import bleach
from typing import Any, Optional, List, Dict
from datetime import datetime
from pydantic import BaseModel, Field, field_validator
from email_validator import validate_email, EmailNotValidError


class SecurityValidators:
    """Security-focused validators for common input types"""

    # Regex patterns for common validations
    PATTERNS = {
        'alphanumeric': re.compile(r'^[a-zA-Z0-9]+$'),
        'alphanumeric_space': re.compile(r'^[a-zA-Z0-9\s]+$'),
        'username': re.compile(r'^[a-zA-Z0-9_.-]{3,50}$'),
        'filename': re.compile(r'^[a-zA-Z0-9._-]+$'),
        'hex_string': re.compile(r'^[a-fA-F0-9]+$'),
        'uuid': re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'),
        'ip_address': re.compile(r'^(?:(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)\.){3}(?:25[0-5]|2[0-4][0-9]|[01]?[0-9][0-9]?)$'),
        'mac_address': re.compile(r'^([0-9A-Fa-f]{2}[:-]){5}([0-9A-Fa-f]{2})$'),
        'url_safe': re.compile(r'^[a-zA-Z0-9._~:/?#[\]@!$&\'()*+,;=-]+$'),
        'sql_injection': re.compile(r'(\b(SELECT|INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|EXEC|UNION|SCRIPT)\b)', re.IGNORECASE),
        'xss_patterns': re.compile(r'(<script|javascript:|vbscript:|onload=|onerror=|onclick=)', re.IGNORECASE)
    }

    @classmethod
    def sanitize_string(cls, value: str, max_length: int = 255, allow_html: bool = False) -> str:
        """Sanitize string input with XSS protection"""
        if not isinstance(value, str):
            raise ValueError("Input must be a string")
        
        # Trim whitespace
        value = value.strip()
        
        # Length validation
        if len(value) > max_length:
            raise ValueError(f"String too long (max {max_length} characters)")
        
        # Check for SQL injection patterns
        if cls.PATTERNS['sql_injection'].search(value):
            raise ValueError("Potentially malicious SQL patterns detected")
        
        # Check for XSS patterns
        if cls.PATTERNS['xss_patterns'].search(value):
            raise ValueError("Potentially malicious script patterns detected")
        
        if not allow_html:
            # HTML encode to prevent XSS
            value = html.escape(value)
        else:
            # Use bleach for safe HTML sanitization
            allowed_tags = ['b', 'i', 'u', 'em', 'strong', 'p', 'br']
            value = bleach.clean(value, tags=allowed_tags, strip=True)
        
        return value

    @classmethod
    def validate_username(cls, username: str) -> str:
        """Validate username with security rules"""
        if not cls.PATTERNS['username'].match(username):
            raise ValueError("Username must be 3-50 characters, alphanumeric with ._- allowed")
        
        # Check for reserved usernames
        reserved = ['admin', 'root', 'user', 'test', 'guest', 'anonymous', 'null', 'undefined']
        if username.lower() in reserved:
            raise ValueError("Username is reserved")
        
        return username.lower()

    @classmethod
    def validate_email(cls, email: str) -> str:
        """Validate email address"""
        try:
            valid = validate_email(email)
            return valid.email
        except EmailNotValidError:
            raise ValueError("Invalid email format")

    @classmethod
    def validate_ip_address(cls, ip: str) -> str:
        """Validate IPv4 address"""
        if not cls.PATTERNS['ip_address'].match(ip):
            raise ValueError("Invalid IP address format")
        return ip

    @classmethod
    def validate_filename(cls, filename: str) -> str:
        """Validate filename for security"""
        if not cls.PATTERNS['filename'].match(filename):
            raise ValueError("Invalid filename - only alphanumeric, dots, dashes, underscores allowed")
        
        # Check for path traversal
        if '..' in filename or '/' in filename or '\\' in filename:
            raise ValueError("Path traversal detected in filename")
        
        # Check length
        if len(filename) > 255:
            raise ValueError("Filename too long")
            
        return filename

    @classmethod
    def validate_integer_range(cls, value: int, min_val: int = None, max_val: int = None) -> int:
        """Validate integer within range"""
        if not isinstance(value, int):
            raise ValueError("Value must be an integer")
        
        if min_val is not None and value < min_val:
            raise ValueError(f"Value must be at least {min_val}")
        
        if max_val is not None and value > max_val:
            raise ValueError(f"Value must be at most {max_val}")
        
        return value

    @classmethod
    def validate_coordinates(cls, lat: float, lon: float) -> tuple[float, float]:
        """Validate geographic coordinates"""
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            raise ValueError("Coordinates must be numeric")
        
        if not (-90 <= lat <= 90):
            raise ValueError("Latitude must be between -90 and 90")
        
        if not (-180 <= lon <= 180):
            raise ValueError("Longitude must be between -180 and 180")
        
        return float(lat), float(lon)


class SecureBaseModel(BaseModel):
    """Base model with built-in security validations"""
    
    model_config = {
        "str_strip_whitespace": True,
        "validate_assignment": True,
        "extra": "forbid"  # Reject unknown fields
    }


class PaginationParams(SecureBaseModel):
    """Secure pagination parameters"""
    page: int = Field(default=1, ge=1, le=1000)
    size: int = Field(default=20, ge=1, le=100)
    
    @field_validator('page', 'size')
    @classmethod
    def validate_positive(cls, v):
        return SecurityValidators.validate_integer_range(v, 1, 1000)


class SearchParams(SecureBaseModel):
    """Secure search parameters"""
    query: Optional[str] = Field(default=None, max_length=100)
    
    @field_validator('query')
    @classmethod
    def validate_search_query(cls, v):
        if v is not None:
            return SecurityValidators.sanitize_string(v, max_length=100)
        return v


class GeolocationParams(SecureBaseModel):
    """Secure geolocation parameters"""
    latitude: Optional[float] = Field(default=None, ge=-90, le=90)
    longitude: Optional[float] = Field(default=None, ge=-180, le=180)
    
    @field_validator('latitude', 'longitude')
    @classmethod
    def validate_coordinates(cls, v):
        if v is not None:
            if not isinstance(v, (int, float)):
                raise ValueError("Coordinate must be numeric")
        return v


class DateRangeParams(SecureBaseModel):
    """Secure date range parameters"""
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    
    @field_validator('end_date')
    @classmethod
    def validate_date_range(cls, v, info):
        if v is not None and 'start_date' in info.data and info.data['start_date'] is not None:
            if v < info.data['start_date']:
                raise ValueError("End date must be after start date")
        return v


class SecurityConfig:
    """Runtime security configuration"""
    
    # Maximum file size (10MB)
    MAX_FILE_SIZE = 10 * 1024 * 1024
    
    # Allowed file extensions
    ALLOWED_EXTENSIONS = {'.pdf', '.doc', '.docx', '.txt', '.csv', '.xlsx', '.png', '.jpg', '.jpeg'}
    
    # Rate limiting
    RATE_LIMIT_PER_MINUTE = 60
    RATE_LIMIT_PER_HOUR = 1000
    
    # Input limits
    MAX_STRING_LENGTH = 1000
    MAX_TEXT_LENGTH = 10000
    MAX_ARRAY_SIZE = 100
    
    @classmethod
    def validate_file_extension(cls, filename: str) -> bool:
        """Validate file extension"""
        import os
        _, ext = os.path.splitext(filename.lower())
        return ext in cls.ALLOWED_EXTENSIONS
    
    @classmethod
    def validate_file_size(cls, size: int) -> bool:
        """Validate file size"""
        return size <= cls.MAX_FILE_SIZE