from typing import Optional, List
from datetime import datetime
from sqlmodel import Field, SQLModel, Relationship
from app.backend.enums import ConnectionStatus


class Post(SQLModel, table=True):
    __tablename__ = "posts"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(max_length=255)
    code: str = Field(unique=True, index=True, max_length=50)
    description: Optional[str] = Field(default=None)
    region: Optional[str] = Field(default=None)
    is_active: bool = Field(default=True, index=True)
    last_connection_at: Optional[datetime] = Field(default=None, index=True)
    connection_status: str = Field(
        default=ConnectionStatus.OFFLINE.value,
        index=True,
        max_length=20
    )
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    # Relationships
    users: List["User"] = Relationship(back_populates="post")
    observations: List["Observation"] = Relationship(back_populates="post")