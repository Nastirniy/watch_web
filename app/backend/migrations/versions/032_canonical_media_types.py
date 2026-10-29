"""Canonical media types: stored types under their IANA names (owner decision 2026-10-28).

The code stores an upload under its format's canonical name since this revision (enums/media_types.py, IANA name
first; services/files/formats.py). Rows written before hold what the sniffer said, which may be an alias
(video/x-matroska, audio/x-wav, image/pjpeg). Through the frozen map migrations/support/media_types_v1.py:
- media_files.mime_type and the base of media_files.playback_type ('video/x-matroska; codecs="..."' ->
  'video/matroska; codecs="..."'), in batches of 10,000 ids, never past the highest id read at the start; first one
  read of the distinct mime_type and playback_type values (up to that id) picks the aliases that occur (compared in
  lower case, as MariaDB's collation compares), and the batches update only those (none occurring: no batch);
- upload_sessions.declared_content_type (a short-lived table: one statement per name);
- only on a row whose file extension belongs to the target format (EXTENSIONS: media_files.file_extension with or
  without the dot, the extension of upload_sessions.original_filename): application/ogg on a .ogv file or
  video/x-matroska on a .webm file is not that format, and keeps its value;
- an alias shared by several default formats (text/plain) by the row's file extension (media_files.file_extension,
  the extension of upload_sessions.original_filename); a row neither decides keeps its value;
- the seeded media_formats rows whose canonical name changed (video/x-matroska -> video/matroska), only while a row
  still holds the old seeded content_type: renamed, the old name becoming its first alias and the new one leaving its
  aliases; an administrator's own value stays, and so does a row whose new name another row already holds
  (content_type is unique; logged). Seeding never updates a row, so without this the existing row kept the x- name
  and the default of the new name was never seeded (its extension is taken);
- the alias lists of the seeded document formats the defaults trimmed (application/zip of docx, xlsx, pptx, odt;
  application/x-ole-storage and application/CDFV2 of doc; application/csv of csv), only while a row's list is still
  exactly the one seeded (a database seeded by the first, unreleased registry commit); an edited list stays.
Values outside the map (an administrator's own formats) stay. No schema change. Every statement commits on its own
(an autocommit block, as 031: no read view across batches on MariaDB with innodb_snapshot_isolation); a second run
changes only rows written in between.

Downgrade is a no-op: the canonical names stay valid for the code of 031 (video/matroska was an alias of its
Matroska format, every other canonical name was a format's own name).

Revision ID: 032_canonical_media_types
Revises: 031_audit_events
Create Date: 2026-10-29
"""
import json

from alembic import op
import sqlalchemy as sa

from app.backend.migrations.support.media_types_v1 import (
    ALIASES, BY_EXTENSION, EXTENSIONS, SEEDED_ALIASES, SEEDED_RENAMES,
)

revision = "032_canonical_media_types"
down_revision = "031_audit_events"
branch_labels = None
depends_on = None

BATCH = 10_000

MEDIA = sa.table("media_files", sa.column("id", sa.Integer), sa.column("mime_type", sa.String),
                 sa.column("playback_type", sa.String), sa.column("file_extension", sa.String))
SESSIONS = sa.table("upload_sessions", sa.column("id", sa.String), sa.column("declared_content_type", sa.String),
                    sa.column("original_filename", sa.String))
FORMATS = sa.table("media_formats", sa.column("id", sa.Integer), sa.column("content_type", sa.String),
                   sa.column("aliases", sa.JSON))


def _log(message: str) -> None:
    print(f"[032_canonical_media_types] {message}")


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    with op.get_context().autocommit_block():
        conn = op.get_bind()
        if "media_files" in tables:
            _media_files(conn)
        if "upload_sessions" in tables:
            _upload_sessions(conn)
        if "media_formats" in tables:
            _seeded_formats(conn)


def downgrade() -> None:
    """No-op: canonical names stay valid (see the module docstring)"""


def _extension_values(*extensions: str) -> list:
    """file_extension as stored: with the dot (every upload since the registry), without it in older rows"""
    return sorted({value for extension in extensions for value in (extension, extension.lstrip("."))})


def _media_of(canonical: str):
    """A media row whose extension belongs to the format of canonical"""
    return sa.func.lower(MEDIA.c.file_extension).in_(_extension_values(*EXTENSIONS[canonical]))


def _session_of(canonical: str):
    """An upload session whose file name ends with an extension of the format of canonical"""
    name = sa.func.lower(SESSIONS.c.original_filename)
    return sa.or_(*[name.like("%" + extension) for extension in EXTENSIONS[canonical]])


def _present(conn, high: int) -> tuple:
    """The aliases of the map that occur up to id high: (in mime_type, as the base of playback_type, of BY_EXTENSION
    in mime_type), each compared in lower case (MariaDB's collation matches case-insensitively; the updates decide)"""
    up_to = MEDIA.c.id <= high
    mime = {value.lower() for (value,) in conn.execute(sa.select(MEDIA.c.mime_type).where(up_to).distinct())
            if value}
    playback = {value.split(";", 1)[0].lower() for (value,) in conn.execute(
        sa.select(MEDIA.c.playback_type).where(up_to, MEDIA.c.playback_type.is_not(None)).distinct()) if value}
    return (sorted(alias for alias in ALIASES if alias.lower() in mime),
            sorted(alias for alias in ALIASES if alias.lower() in playback),
            sorted(alias for alias in BY_EXTENSION if alias.lower() in mime))


def _media_files(conn) -> None:
    low, high = conn.execute(sa.select(sa.func.min(MEDIA.c.id), sa.func.max(MEDIA.c.id))).one()
    if low is None:
        return
    in_mime, in_playback, shared = _present(conn, high)
    _log(f"media_files aliases found: mime_type {in_mime + shared}, playback_type {in_playback}")
    if not (in_mime or in_playback or shared):
        return
    start, batches, changed = low, 0, 0
    while start <= high:
        end = min(start + BATCH - 1, high)
        in_batch = MEDIA.c.id.between(start, end)
        for alias in in_mime:
            changed += conn.execute(sa.update(MEDIA).where(in_batch, MEDIA.c.mime_type == alias,
                                                           _media_of(ALIASES[alias]))
                                    .values(mime_type=ALIASES[alias])).rowcount or 0
        for alias in in_playback:
            canonical = ALIASES[alias]
            # the base of the RFC 6381 string: the alias alone or followed by its parameters
            rest = sa.func.substr(MEDIA.c.playback_type, len(alias) + 1, type_=sa.String)
            conn.execute(sa.update(MEDIA).where(
                in_batch, _media_of(canonical),
                sa.or_(MEDIA.c.playback_type == alias, MEDIA.c.playback_type.like(alias + ";%")))
                .values(playback_type=sa.literal(canonical, sa.String) + rest))
        for alias in shared:
            for extension, canonical in sorted(BY_EXTENSION[alias].items()):
                changed += conn.execute(sa.update(MEDIA).where(
                    in_batch, MEDIA.c.mime_type == alias,
                    sa.func.lower(MEDIA.c.file_extension).in_(_extension_values(extension)))
                    .values(mime_type=canonical)).rowcount or 0
        start, batches = end + 1, batches + 1
    _log(f"media_files ids {low}..{high} in {batches} batches: {changed} types made canonical")


def _upload_sessions(conn) -> None:
    changed = 0
    for alias, canonical in sorted(ALIASES.items()):
        changed += conn.execute(sa.update(SESSIONS).where(SESSIONS.c.declared_content_type == alias,
                                                          _session_of(canonical))
                                .values(declared_content_type=canonical)).rowcount or 0
    for alias, by_extension in sorted(BY_EXTENSION.items()):
        for extension, canonical in sorted(by_extension.items()):
            changed += conn.execute(sa.update(SESSIONS).where(
                SESSIONS.c.declared_content_type == alias,
                sa.func.lower(SESSIONS.c.original_filename).like("%" + extension))
                .values(declared_content_type=canonical)).rowcount or 0
    _log(f"upload_sessions: {changed} declared types made canonical")


def _aliases(value) -> list:
    if isinstance(value, str):     # a JSON column read without its type (SQLite) arrives as text
        value = json.loads(value)
    return list(value or [])


def _seeded_formats(conn) -> None:
    rows = conn.execute(sa.select(FORMATS.c.id, FORMATS.c.content_type, FORMATS.c.aliases)).all()
    taken = {content_type for _, content_type, _ in rows}
    for row_id, content_type, aliases in rows:
        new_name = SEEDED_RENAMES.get(content_type)
        if new_name is None:
            continue
        if new_name in taken:
            _log(f"media format {row_id} keeps {content_type}: {new_name} already taken by another row")
            continue
        renamed = [content_type] + [alias for alias in _aliases(aliases) if alias not in (content_type, new_name)]
        conn.execute(sa.update(FORMATS).where(FORMATS.c.id == row_id, FORMATS.c.content_type == content_type)
                     .values(content_type=new_name, aliases=renamed))
        taken.discard(content_type)
        taken.add(new_name)
        _log(f"media format {row_id}: {content_type} -> {new_name}")
    for row_id, content_type, aliases in rows:
        seeded = SEEDED_ALIASES.get(content_type)
        if seeded is not None and _aliases(aliases) == seeded[0]:
            conn.execute(sa.update(FORMATS).where(FORMATS.c.id == row_id, FORMATS.c.content_type == content_type)
                         .values(aliases=list(seeded[1])))
            _log(f"media format {row_id} ({content_type}): aliases {seeded[0]} -> {seeded[1]}")
