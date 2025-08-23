"""
Post model for monitoring stations/locations.
Represents physical locations where signal monitoring occurs.
"""

from datetime import datetime
from enum import Enum
from typing import List, Optional, TYPE_CHECKING

from sqlmodel import Field, Relationship

from backend.database.models.base import BaseModel, StatusMixin

if TYPE_CHECKING:
    from backend.database.models.user import User
    from backend.database.models.signal import Observation


class ConnectionStatus(str, Enum):
    """Post connection status enumeration."""
    ONLINE = "online"
    OFFLINE = "offline"
    MAINTENANCE = "maintenance"
    ERROR = "error"


class Post(BaseModel, StatusMixin, table=True):
    """Monitoring post/station model."""

    __tablename__ = "posts"

    id: Optional[int] = Field(default=None, primary_key=True)

    # Basic information
    name: str = Field(max_length=255, description="Post name")
    code: str = Field(
        max_length=50,
        unique=True,
        index=True,
        description="Unique post code identifier"
    )
    description: Optional[str] = Field(default=None, description="Post description")

    # Location information
    region: Optional[str] = Field(default=None, description="Geographic region")
    latitude: Optional[float] = Field(default=None, description="GPS latitude")
    longitude: Optional[float] = Field(default=None, description="GPS longitude")
    altitude: Optional[float] = Field(default=None, description="Altitude in meters")

    # Connection and status tracking
    last_connection_at: Optional[datetime] = Field(
        default=None,
        index=True,
        description="Last connection timestamp"
    )
    connection_status: ConnectionStatus = Field(
        default=ConnectionStatus.OFFLINE,
        index=True,
        description="Current connection status"
    )

    # Equipment information
    equipment_info: Optional[str] = Field(default=None, description="Equipment description")
    frequency_range_min: Optional[float] = Field(default=None, description="Minimum frequency range (MHz)")
    frequency_range_max: Optional[float] = Field(default=None, description="Maximum frequency range (MHz)")

    # Contact information
    contact_person: Optional[str] = Field(default=None, description="Contact person name")
    contact_phone: Optional[str] = Field(default=None, description="Contact phone number")
    contact_email: Optional[str] = Field(default=None, description="Contact email address")

    # Relationships
    users: List["User"] = Relationship(back_populates="post")
    observations: List["Observation"] = Relationship(back_populates="post")

    @property
    def is_online(self) -> bool:
        """Check if post is currently online."""
        return self.connection_status == ConnectionStatus.ONLINE

    @property
    def coordinates(self) -> Optional[tuple[float, float]]:
        """Get coordinates as tuple (latitude, longitude)."""
        if self.latitude is not None and self.longitude is not None:
            return (self.latitude, self.longitude)
        return None

    def update_connection_status(self, status: ConnectionStatus) -> None:
        """Update connection status and timestamp."""
        self.connection_status = status
        if status == ConnectionStatus.ONLINE:
            self.last_connection_at = datetime.utcnow()
