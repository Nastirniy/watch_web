"""MediaFile.storyboard_job_status, attached after every model is imported.

Called once from app.backend.models.__init__ (same pattern as presence_properties).
"""
from sqlalchemy import inspect, select as sa_select
from sqlalchemy.orm import column_property

from app.backend.models.media import MediaFile
from app.backend.models.storyboard import VideoStoryboard

_configured = False


def configure_storyboard_properties() -> None:
    global _configured
    if _configured:
        return
    _configured = True

    # Status of the storyboard job of the video, None while it has no row
    inspect(MediaFile).add_property(
        "storyboard_job_status",
        column_property(
            sa_select(VideoStoryboard.status)
            .where(VideoStoryboard.media_id == MediaFile.id)
            .correlate_except(VideoStoryboard)
            .scalar_subquery()
        ),
    )
