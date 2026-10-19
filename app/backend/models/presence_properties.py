"""Derived presence columns attached to the ORM models after every model is imported.

Called once from app.backend.models.__init__. Kept outside the model modules
so that user.py and post.py stay free of cross imports.
"""
from sqlalchemy import func, inspect, select as sa_select
from sqlalchemy.orm import column_property

from app.backend.models.user import User, UserSession
from app.backend.models.post import Post
from app.backend.auth.presence import online_cutoff_param
from app.backend.auth.sessions import live_clause

_configured = False


def configure_presence_properties() -> None:
    global _configured
    if _configured:
        return
    _configured = True

    # User.last_activity_at: latest activity over the user's live sessions (live_clause: not ended by logout or
    # revocation, nor by the computation: idle timeout, hard end). User.is_online compares it with the online
    # cutoff, so it agrees with the is_online filter (presence.online_user_ids_select)
    inspect(User).add_property(
        "last_activity_at",
        column_property(
            sa_select(func.max(UserSession.last_activity))
            .where(UserSession.user_id == User.id, live_clause())
            .correlate_except(UserSession)
            .scalar_subquery()
        ),
    )

    # User.last_login_at: the latest login session, ended or not (every login opens one). Derived instead of stored so
    # that a login never writes the users row: a write there collided with the user's concurrent requests on MariaDB
    # (error 1020 on their foreign key checks, a deadlock between two logins); migration 026 dropped the column
    inspect(User).add_property(
        "last_login_at",
        column_property(
            sa_select(func.max(UserSession.created_at))
            .where(UserSession.user_id == User.id)
            .correlate_except(UserSession)
            .scalar_subquery()
        ),
    )

    # Post.online_user_count: home users of the post with a session touched within the threshold
    inspect(Post).add_property(
        "online_user_count",
        column_property(
            sa_select(func.count(func.distinct(UserSession.user_id)))
            .select_from(UserSession)
            .join(User, User.id == UserSession.user_id)
            .where(
                User.post_id == Post.id,
                live_clause(),
                UserSession.last_activity >= online_cutoff_param(),
            )
            .correlate_except(UserSession, User)
            .scalar_subquery()
        ),
    )

    # Post.user_count: active, not archived home users of the post
    inspect(Post).add_property(
        "user_count",
        column_property(
            sa_select(func.count(User.id))
            .where(User.post_id == Post.id, User.is_active == True, User.is_deleted == False)
            .correlate_except(User)
            .scalar_subquery()
        ),
    )

    # Post.last_user_activity_at: latest activity of any home user of the post (the largest last_activity_at of
    # its users: live sessions only)
    inspect(Post).add_property(
        "last_user_activity_at",
        column_property(
            sa_select(func.max(UserSession.last_activity))
            .select_from(UserSession)
            .join(User, User.id == UserSession.user_id)
            .where(User.post_id == Post.id, live_clause())
            .correlate_except(UserSession, User)
            .scalar_subquery()
        ),
    )
