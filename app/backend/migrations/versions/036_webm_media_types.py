"""WebM files stored as Matroska: rows of .webm files under the name of their format.

libmagic called a WebM file video/x-matroska, and rows written before 032 hold that. 032 rewrote that alias only on
.mkv files (the Matroska format), so these rows still say Matroska, and what the API reports for the file with them.
Through the frozen map migrations/support/media_types_v2.py, on media_files rows whose file_extension is .webm (with or
without the dot, any case):
- mime_type video/x-matroska becomes video/webm;
- the base of playback_type follows ('video/x-matroska; codecs="vp9"' -> 'video/webm; codecs="vp9"', the RFC 6381
  string the probe wrote), also on a row whose mime_type is already video/webm.
In batches of 10,000 ids, never past the highest id read at the start; first one read of the distinct values up to that
id: nothing to do runs no batch. Every other row keeps its data: other extensions, the codecs of the string, any other
column; upload_sessions.declared_content_type stays, a short-lived table the session ends with. No schema change. Every
statement commits on its own (an autocommit block, as 031 and 032: no read view across batches on MariaDB with
innodb_snapshot_isolation); a second run changes only rows written in between.

Downgrade is a no-op: video/webm is the WebM format's own name, valid for the code of 035, and a row no longer tells
what it held before.

Revision ID: 036_webm_media_types
Revises: 035_queued_moves
Create Date: 2026-10-30
"""
from alembic import op
import sqlalchemy as sa

from app.backend.migrations.support.media_types_v2 import OLD_NAME, WEBM, WEBM_EXTENSIONS

revision = "036_webm_media_types"
down_revision = "035_queued_moves"
branch_labels = None
depends_on = None

BATCH = 10_000

MEDIA = sa.table("media_files", sa.column("id", sa.Integer), sa.column("mime_type", sa.String),
                 sa.column("playback_type", sa.String), sa.column("file_extension", sa.String))


def _log(message: str) -> None:
    print(f"[036_webm_media_types] {message}")


def upgrade() -> None:
    if "media_files" not in sa.inspect(op.get_bind()).get_table_names():
        return
    with op.get_context().autocommit_block():
        _media_files(op.get_bind())


def downgrade() -> None:
    """No-op: the WebM name stays valid (see the module docstring)"""


def _is_webm():
    """file_extension as stored: with the dot (every upload since the registry), without it in older rows"""
    values = sorted({value for extension in WEBM_EXTENSIONS for value in (extension, extension.lstrip("."))})
    return sa.func.lower(MEDIA.c.file_extension).in_(values)


def _playback_is_old():
    """The base of the RFC 6381 string is the old name: alone or followed by its parameters"""
    return sa.or_(MEDIA.c.playback_type == OLD_NAME, MEDIA.c.playback_type.like(OLD_NAME + ";%"))


def _media_files(conn) -> None:
    low, high = conn.execute(sa.select(sa.func.min(MEDIA.c.id), sa.func.max(MEDIA.c.id))).one()
    if low is None:
        return
    found = conn.execute(sa.select(MEDIA.c.id).where(
        MEDIA.c.id <= high, _is_webm(), sa.or_(MEDIA.c.mime_type == OLD_NAME, _playback_is_old())).limit(1)).first()
    if found is None:
        _log(f"no row holds {OLD_NAME} on a WebM file")
        return
    start, batches, changed = low, 0, 0
    # the old name's length is the same whatever case the database compares in
    rest = sa.func.substr(MEDIA.c.playback_type, len(OLD_NAME) + 1, type_=sa.String)
    while start <= high:
        end = min(start + BATCH - 1, high)
        in_batch = MEDIA.c.id.between(start, end)
        changed += conn.execute(sa.update(MEDIA).where(in_batch, MEDIA.c.mime_type == OLD_NAME, _is_webm())
                                .values(mime_type=WEBM)).rowcount or 0
        conn.execute(sa.update(MEDIA).where(in_batch, _is_webm(), _playback_is_old())
                     .values(playback_type=sa.literal(WEBM, sa.String) + rest))
        start, batches = end + 1, batches + 1
    _log(f"media_files ids {low}..{high} in {batches} batches: {changed} types made video/webm")
