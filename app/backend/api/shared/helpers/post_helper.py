"""Archived posts: the data of an archived post is read-only history.

The read side needs nothing here: role assignments made on an archived post stop applying, assignments inherited
from a group or the root keep read access (auth/scope.py, nodes_in_effect_at_post). The write side is
refuse_archived_post: every write into the data of a post (observations, bands, tags, media, uploads, posters) and
making it a user's home post answers 409 post_archived. Call it after the scope and ownership checks, so an outsider
still gets its 403 first.
"""
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.models.post import Post
from app.backend.errors.problems import ApiError
from app.backend.enums.errors import ErrorCode


def refuse_archived_post(post: Post) -> None:
    """409 post_archived when the loaded post is archived"""
    if post.is_deleted:
        raise ApiError(ErrorCode.POST_ARCHIVED, post_id=post.id)


async def ensure_post_not_archived(session: AsyncSession, post_id: int) -> None:
    """409 post_archived when the post is archived; a missing post passes (the caller answers its own 404)"""
    if (await session.exec(select(Post.is_deleted).where(Post.id == post_id))).first():
        raise ApiError(ErrorCode.POST_ARCHIVED, post_id=post_id)
