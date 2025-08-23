"""
Post management schemas for monitoring stations and locations.
Handles post creation, updates, and location-based operations.
"""

from datetime import datetime
from typing import List, Optional, Tuple

from pydantic import Field, field_validator, EmailStr

from backend.database.models import ConnectionStatus
from backend.api.schemas.base import BaseSchema, TimestampSchema, SearchFilters


class PostBase(BaseSchema):
    """Base post schema."""

    name: str = Field(description="Post name")
    code: str = Field(description="Unique post code identifier")
    description: Optional[str] = Field(None, description="Post description")
    region: Optional[str] = Field(None, description="Geographic region")
    latitude: Optional[float] = Field(None, description="GPS latitude")
    longitude: Optional[float] = Field(None, description="GPS longitude")
    altitude: Optional[float] = Field(None, description="Altitude in meters")
    is_active: bool = Field(default=True, description="Active status")
    equipment_info: Optional[str] = Field(None, description="Equipment description")
    frequency_range_min: Optional[float] = Field(None, description="Minimum frequency range (MHz)")
    frequency_range_max: Optional[float] = Field(None, description="Maximum frequency range (MHz)")
    contact_person: Optional[str] = Field(None, description="Contact person name")
    contact_phone: Optional[str] = Field(None, description="Contact phone number")
    contact_email: Optional[EmailStr] = Field(None, description="Contact email address")


class PostCreate(PostBase):
    """Schema for creating posts."""

    @field_validator('code')
    @classmethod
    def validate_code(cls, v: str) -> str:
        if not v or len(v.strip()) < 2:
            raise ValueError("Post code must be at least 2 characters")
        if not v.replace('-', '').replace('_', '').isalnum():
            raise ValueError("Post code can only contain letters, numbers, hyphens, and underscores")
        return v.upper().strip()

    @field_validator('latitude')
    @classmethod
    def validate_latitude(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and (v < -90 or v > 90):
            raise ValueError("Latitude must be between -90 and 90 degrees")
        return v

    @field_validator('longitude')
    @classmethod
    def validate_longitude(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and (v < -180 or v > 180):
            raise ValueError("Longitude must be between -180 and 180 degrees")
        return v

    @field_validator('frequency_range_min', 'frequency_range_max')
    @classmethod
    def validate_frequency_range(cls, v: Optional[float]) -> Optional[float]:
        if v is not None and v <= 0:
            raise ValueError("Frequency range must be positive")
        return v


class PostUpdate(BaseSchema):
    """Schema for updating posts."""

    name: Optional[str] = Field(None, description="Post name")
    description: Optional[str] = Field(None, description="Post description")
    region: Optional[str] = Field(None, description="Geographic region")
    latitude: Optional[float] = Field(None, description="GPS latitude")
    longitude: Optional[float] = Field(None, description="GPS longitude")
    altitude: Optional[float] = Field(None, description="Altitude in meters")
    is_active: Optional[bool] = Field(None, description="Active status")
    equipment_info: Optional[str] = Field(None, description="Equipment description")
    frequency_range_min: Optional[float] = Field(None, description="Minimum frequency range (MHz)")
    frequency_range_max: Optional[float] = Field(None, description="Maximum frequency range (MHz)")
    contact_person: Optional[str] = Field(None, description="Contact person name")
    contact_phone: Optional[str] = Field(None, description="Contact phone number")
    contact_email: Optional[EmailStr] = Field(None, description="Contact email address")


class Post(PostBase, TimestampSchema):
    """Post response schema."""

    id: int = Field(description="Post ID")
    last_connection_at: Optional[datetime] = Field(None, description="Last connection timestamp")
    connection_status: ConnectionStatus = Field(description="Current connection status")
    user_count: Optional[int] = Field(None, description="Number of assigned users")
    observation_count: Optional[int] = Field(None, description="Number of observations")
    last_observation_at: Optional[datetime] = Field(None, description="Last observation timestamp")

    @property
    def coordinates(self) -> Optional[Tuple[float, float]]:
        """Get coordinates as tuple (latitude, longitude)."""
        if self.latitude is not None and self.longitude is not None:
            return (self.latitude, self.longitude)
        return None

    @property
    def is_online(self) -> bool:
        """Check if post is currently online."""
        return self.connection_status == ConnectionStatus.ONLINE


class PostSummary(BaseSchema):
    """Simplified post schema for listings."""

    id: int = Field(description="Post ID")
    name: str = Field(description="Post name")
    code: str = Field(description="Post code")
    region: Optional[str] = Field(None, description="Geographic region")
    is_active: bool = Field(description="Active status")
    connection_status: ConnectionStatus = Field(description="Connection status")
    last_connection_at: Optional[datetime] = Field(None, description="Last connection timestamp")
    user_count: Optional[int] = Field(None, description="Number of assigned users")
    observation_count: Optional[int] = Field(None, description="Recent observation count")


class PostSearchFilters(SearchFilters):
    """Post search filters."""

    region: Optional[str] = Field(None, description="Filter by region")
    connection_status: Optional[ConnectionStatus] = Field(None, description="Filter by connection status")
    is_active: Optional[bool] = Field(None, description="Filter by active status")
    has_users: Optional[bool] = Field(None, description="Filter by user assignment")
    has_recent_activity: Optional[bool] = Field(None, description="Filter by recent activity")
    latitude_min: Optional[float] = Field(None, description="Minimum latitude for geo search")
    latitude_max: Optional[float] = Field(None, description="Maximum latitude for geo search")
    longitude_min: Optional[float] = Field(None, description="Minimum longitude for geo search")
    longitude_max: Optional[float] = Field(None, description="Maximum longitude for geo search")


class ConnectionStatusUpdate(BaseSchema):
    """Schema for updating post connection status."""

    status: ConnectionStatus = Field(description="New connection status")
    timestamp: Optional[datetime] = Field(None, description="Status change timestamp")
    metadata: Optional[dict] = Field(None, description="Additional status metadata")


class BulkStatusUpdate(BaseSchema):
    """Bulk status update schema."""

    post_ids: List[int] = Field(description="Post IDs to update")
    status: ConnectionStatus = Field(description="New connection status")
    reason: Optional[str] = Field(None, description="Reason for status change")


class PostStatistics(BaseSchema):
    """Post statistics schema."""

    total_posts: int = Field(description="Total number of posts")
    active_posts: int = Field(description="Number of active posts")
    by_status: dict = Field(default_factory=dict, description="Count by connection status")
    by_region: dict = Field(default_factory=dict, description="Count by region")
    with_users: int = Field(description="Posts with assigned users")
    with_recent_activity: int = Field(description="Posts with recent activity")
    average_observations_per_post: float = Field(description="Average observations per post")


class LocationSearch(BaseSchema):
    """Location-based search parameters."""

    latitude: float = Field(description="Search center latitude")
    longitude: float = Field(description="Search center longitude")
    radius_km: float = Field(description="Search radius in kilometers", gt=0, le=1000)
    include_inactive: bool = Field(default=False, description="Include inactive posts")


class PostActivity(BaseSchema):
    """Post activity summary."""

    post_id: int = Field(description="Post ID")
    post_name: str = Field(description="Post name")
    post_code: str = Field(description="Post code")
    connection_status: ConnectionStatus = Field(description="Connection status")
    last_connection_at: Optional[datetime] = Field(None, description="Last connection")
    uptime_percentage: Optional[float] = Field(None, description="Uptime percentage")
    observation_count_24h: int = Field(default=0, description="Observations in last 24 hours")
    observation_count_7d: int = Field(default=0, description="Observations in last 7 days")
    observation_count_30d: int = Field(default=0, description="Observations in last 30 days")
    unique_signals_24h: int = Field(default=0, description="Unique signals in last 24 hours")
    last_observation_at: Optional[datetime] = Field(None, description="Last observation timestamp")
    assigned_users: List[str] = Field(default_factory=list, description="Assigned user names")


class PostConfiguration(BaseSchema):
    """Post configuration schema."""

    post_id: int = Field(description="Post ID")
    monitoring_enabled: bool = Field(default=True, description="Monitoring enabled")
    auto_classification: bool = Field(default=False, description="Auto classification enabled")
    alert_thresholds: dict = Field(default_factory=dict, description="Alert thresholds")
    frequency_scan_ranges: List[dict] = Field(default_factory=list, description="Frequency scan ranges")
    scan_interval_seconds: int = Field(default=60, description="Scan interval in seconds")
    data_retention_days: int = Field(default=90, description="Data retention period")
    quality_thresholds: dict = Field(default_factory=dict, description="Data quality thresholds")


class PostMaintenanceSchedule(BaseSchema):
    """Post maintenance schedule."""

    post_id: int = Field(description="Post ID")
    maintenance_type: str = Field(description="Type of maintenance")
    scheduled_start: datetime = Field(description="Scheduled start time")
    scheduled_end: datetime = Field(description="Scheduled end time")
    description: str = Field(description="Maintenance description")
    contact_person: Optional[str] = Field(None, description="Maintenance contact person")
    expected_downtime: bool = Field(default=True, description="Expected downtime during maintenance")
    notification_sent: bool = Field(default=False, description="Notification sent to users")


class PostHealthCheck(BaseSchema):
    """Post health check result."""

    post_id: int = Field(description="Post ID")
    timestamp: datetime = Field(description="Health check timestamp")
    is_healthy: bool = Field(description="Overall health status")
    connection_ok: bool = Field(description="Connection status")
    data_quality_ok: bool = Field(description="Data quality status")
    equipment_ok: bool = Field(description="Equipment status")
    disk_space_ok: bool = Field(description="Disk space status")
    memory_ok: bool = Field(description="Memory status")
    cpu_ok: bool = Field(description="CPU status")
    temperature_ok: bool = Field(description="Temperature status")
    details: dict = Field(default_factory=dict, description="Detailed health metrics")
    alerts: List[str] = Field(default_factory=list, description="Active alerts")


class RegionSummary(BaseSchema):
    """Regional summary statistics."""

    region: str = Field(description="Region name")
    post_count: int = Field(description="Number of posts in region")
    active_post_count: int = Field(description="Number of active posts")
    online_post_count: int = Field(description="Number of online posts")
    total_observations: int = Field(description="Total observations in region")
    unique_signal_types: int = Field(description="Unique signal types observed")
    coverage_area_km2: Optional[float] = Field(None, description="Estimated coverage area")
    last_activity: Optional[datetime] = Field(None, description="Last activity in region")
