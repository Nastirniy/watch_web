from typing import Optional, TYPE_CHECKING
from datetime import datetime, timezone
from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel, Relationship

if TYPE_CHECKING:
    from app.backend.models.user import User
    from app.backend.models.post import Post


class UserPostAccess(SQLModel, table=True):
    """Additional post access granted to a user besides the home post (users.post_id).

    can_write=False gives read access to the post data (observations, media, users);
    can_write=True additionally allows creating and updating data in that post.
    """
    __tablename__ = "user_post_access"
    __table_args__ = (
        UniqueConstraint("user_id", "post_id", name="uq_user_post_access_user_post"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    post_id: int = Field(foreign_key="posts.id", index=True)
    can_write: bool = Field(default=False)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    # Relationships
    user: "User" = Relationship(back_populates="post_access")
    post: "Post" = Relationship(back_populates="user_access")
