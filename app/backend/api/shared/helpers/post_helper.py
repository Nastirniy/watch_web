"""Archived posts: the data of an archived post is read-only history.

The read side needs nothing here: an archived post leaves every data scope that came from a home post or a
user_post_access row (auth/context.py, _legacy_data_scope), so only post.access_all still reads it. The write side is
refuse_archived_post: every write into the data of a post (observations, bands, tags, media, uploads, posters) and
every assignment of a post (home post, access row) answers 409 post_archived. Call it after the scope and ownership
checks, so an outsider still gets its 403 first.
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
