"""
DateTime utility functions for timezone handling and date operations
Uses only Python's standard library zoneinfo for consistency
"""

from typing import Optional
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.backend.config import settings


def _get_timezone(timezone_name: str):
    """
    Get timezone object with UTC fallback.
    Uses only standard library zoneinfo for consistency.
    """
    if timezone_name.upper() == 'UTC':
        return timezone.utc
    
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError:
        # Log warning and fallback to UTC
        print(f"WARNING: Timezone '{timezone_name}' not found, falling back to UTC")
        return timezone.utc


def get_default_timezone_datetime(dt: Optional[datetime] = None) -> datetime:
    """
    Handle datetime with timezone support based on project settings.
    
    Args:
        dt: Optional datetime. If provided, use it as-is (raw).
            If None, use current time in configured timezone.
    
    Returns:
        datetime: The processed datetime value
    """
    if dt is None:
        # Use current time with configured timezone
        tz = _get_timezone(settings.timezone.timezone)
        return datetime.now(tz).astimezone(timezone.utc)
    
    # If datetime is provided, use it as raw without modification
    return dt


def localize_datetime_to_timezone(dt: datetime, timezone_name: str) -> datetime:
    """
    Convert a datetime to a specific timezone.
    
    Args:
        dt: The datetime to convert
        timezone_name: Target timezone name (e.g., 'UTC', 'Europe/Kyiv')
    
    Returns:
        datetime: The datetime converted to the target timezone
    """
    if dt.tzinfo is None:
        # If naive datetime, assume it's in UTC
        dt = dt.replace(tzinfo=timezone.utc)
    
    target_tz = _get_timezone(timezone_name)
    return dt.astimezone(target_tz)


def get_current_time_in_timezone(timezone_name: str) -> datetime:
    """
    Get current time in the specified timezone.
    
    Args:
        timezone_name: Target timezone name
        
    Returns:
        datetime: Current time in the specified timezone
    """
    tz = _get_timezone(timezone_name)
    return datetime.now(tz)


def is_timezone_valid(timezone_name: str) -> bool:
    """
    Check if a timezone name is valid.
    
    Args:
        timezone_name: Timezone name to validate
        
    Returns:
        bool: True if timezone is valid, False otherwise
    """
    if timezone_name.upper() == 'UTC':
        return True
        
    try:
        ZoneInfo(timezone_name)
        return True
    except ZoneInfoNotFoundError:
        return False