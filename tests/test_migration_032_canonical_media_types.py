"""032_canonical_media_types: stored media types become the canonical (IANA first) names through the frozen map of
migrations/support/media_types_v1.py: media_files.mime_type and the base of playback_type in batches of 10,000 ids,
upload_sessions.declared_content_type, and the seeded media_formats rows whose canonical name changed (only while they
still hold the old seeded value). Values outside the map stay; downgrade changes nothing."""
import json
import sqlite3
from pathlib import Path

import pytest
from alembic.script import ScriptDirectory

from app.backend.migrations.support import media_types_v1
from app.backend.services.files import formats
from tests.test_column_bounds import _run_downgrade
from tests.test_migrations import HEAD, PROJECT_ROOT, run_migrate

pytestmark = pytest.mark.slow

REVISION = "032_canonical_media_types"
PREVIOUS = "031_audit_events"
STAMP = "2026-10-01 10:00:00"
FAR_ID = 25_003     # a row two batches away from the others

# id: (mime_type, playback_type, file_extension) -> (mime_type, playback_type) after 032
MEDIA = {
    1: (("video/x-matroska", 'video/x-matroska; codecs="avc1.640015, mp4a.40.2"', ".mkv"),
        ("video/matroska", 'video/matroska; codecs="avc1.640015, mp4a.40.2"')),
    2: (("audio/x-wav", 'audio/x-wav; codecs="1"', ".wav"), ("audio/wav", 'audio/wav; codecs="1"')),
    3: (("image/pjpeg", None, ".jpg"), ("image/jpeg", None)),
    4: (("video/mp4", 'video/mp4; codecs="avc1.64001F"', ".mp4"), ("video/mp4", 'video/mp4; codecs="avc1.64001F"')),
    5: (("image/heic", None, ".heic"), ("image/heic", None)),                # an admin's format: not in the map
    6: (("text/plain", None, ".md"), ("text/markdown", None)),               # shared alias: the extension decides
    7: (("text/plain", None, ".txt"), ("text/plain", None)),
    8: (("text/plain", None, ".log"), ("text/plain", None)),                 # neither decides: unchanged
    9: (("audio/mp3", "audio/mp3", ".mp3"), ("audio/mpeg", "audio/mpeg")),
    10: (("audio/x-wavpack", "audio/x-wavpack", ".wv"), ("audio/x-wavpack", "audio/x-wavpack")),   # not a prefix match
    11: (("audio/x-m4a", 'audio/x-m4a; codecs="mp4a.40.2"', ".m4a"), ("audio/mp4", 'audio/mp4; codecs="mp4a.40.2"')),
    # an alias on a file of another format: the extension does not belong to the target format, 032 leaves the row and
    # 036 makes it what it is (these tests migrate to the head)
    12: (("video/x-matroska", 'video/x-matroska; codecs="vp9"', ".webm"), ("video/webm", 'video/webm; codecs="vp9"')),
    13: (("application/ogg", 'application/ogg; codecs="theora"', ".ogv"), ("application/ogg", 'application/ogg; codecs="theora"')),
    14: (("image/pjpeg", None, "jpeg"), ("image/jpeg", None)),             # an older row: extension without the dot
    FAR_ID: (("video/x-m4v", 'video/x-m4v; codecs="avc1"', ".m4v"), ("video/mp4", 'video/mp4; codecs="avc1"')),
}
# id: (original_filename, declared_content_type) -> declared_content_type after 032
SESSIONS = {
    "u1": (("clip.mkv", "video/x-matroska"), "video/matroska"),
    "u2": (("clip.mkv", None), None),
    "u3": (("clip.mkv", "application/octet-stream"), "application/octet-stream"),
    "u4": (("Guide.MD", "text/plain"), "text/markdown"),
    "u5": (("photo.jpg", "image/pjpeg"), "image/jpeg"),
    "u6": (("clip.webm", "video/x-matroska"), "video/x-matroska"),        # not a Matroska format file: stays
}


def _insert_media(conn, row_id, mime_type, playback_type, extension):
    conn.execute("insert into media_files (id, observation_id, original_filename, stored_filename, file_path,"
                 " file_size, mime_type, file_extension, media_type, file_hash, poster_attempts, uploaded_at,"
                 " is_deleted, playback_type) values (?, 1, ?, ?, ?, 1, ?, ?, 'video', 'h', 0, ?, 0, ?)",
                 (row_id, f"a{extension}", f"{row_id}{extension}", f"{row_id}{extension}", mime_type, extension,
                  STAMP, playback_type))


def _insert_format(conn, row_id, media_type, content_type, extensions, aliases):
    conn.execute("insert into media_formats (id, media_type, content_type, label, extensions, aliases,"
                 " max_file_size_bytes, is_active, sort_order, is_deleted, created_at, updated_at)"
                 " values (?, ?, ?, ?, ?, ?, 1000, 1, ?, 0, ?, ?)",
                 (row_id, media_type, content_type, content_type, json.dumps(extensions), json.dumps(aliases), row_id,
                  STAMP, STAMP))


def _database_at_031(tmp_path: Path, formats_rows) -> Path:
    db_file = tmp_path / "media_types.db"
    first = run_migrate(db_file)
    assert first.returncode == 0, first.stdout + first.stderr
    down = _run_downgrade(db_file, PREVIOUS)
    assert down.returncode == 0, down.stdout + down.stderr
    conn = sqlite3.connect(db_file)
    for row_id, ((mime_type, playback_type, extension), _) in MEDIA.items():
        _insert_media(conn, row_id, mime_type, playback_type, extension)
    for session_id, ((filename, declared), _) in SESSIONS.items():
        conn.execute("insert into upload_sessions (id, user_id, observation_id, post_id, media_type,"
                     " original_filename, declared_content_type, expected_size, received_bytes, status, part_path,"
                     " created_at, updated_at, last_activity_at, expires_at)"
                     " values (?, 1, 1, 1, 'video', ?, ?, 10, 0, 'open', 'p', ?, ?, ?, ?)",
                     (session_id, filename, declared, STAMP, STAMP, STAMP, STAMP))
    for row in formats_rows:
        _insert_format(conn, *row)
    conn.commit()
    conn.close()
    return db_file


def _media(db_file: Path) -> dict:
    conn = sqlite3.connect(db_file)
    rows = {row[0]: (row[1], row[2]) for row in conn.execute("select id, mime_type, playback_type from media_files")}
    conn.close()
    return rows


def _sessions(db_file: Path) -> dict:
    conn = sqlite3.connect(db_file)
    rows = dict(conn.execute("select id, declared_content_type from upload_sessions").fetchall())
    conn.close()
    return rows


def _formats(db_file: Path) -> dict:
    conn = sqlite3.connect(db_file)
    rows = {row[0]: (row[1], json.loads(row[2])) for row in conn.execute(
        "select id, content_type, aliases from media_formats")}
    conn.close()
    return rows


SEEDED_AT_031 = [   # the media rows as seeding wrote them before 032 (the matroska row under its x- name)
    (1, "image", "image/jpeg", [".jpg", ".jpeg"], ["image/pjpeg"]),
    (2, "video", "video/mp4", [".mp4", ".m4v"], ["video/x-m4v"]),
    (3, "video", "video/x-matroska", [".mkv"], ["video/matroska", "video/x-mkv"]),     # an admin added an alias
    (4, "audio", "audio/wav", [".wav"], ["audio/x-wav", "audio/wave", "audio/vnd.wave"]),
    # documents as the first registry commit seeded them: the trimmed lists are replaced, an edited one stays
    (5, "document", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", [".docx"], ["application/zip"]),
    (6, "document", "application/msword", [".doc"], ["application/x-ole-storage"]),
    (7, "document", "text/csv", [".csv"], ["text/plain", "application/csv"]),
]


def test_032_is_in_the_chain():
    scripts = ScriptDirectory(str(PROJECT_ROOT / "app" / "backend" / "migrations"))
    assert scripts.get_revision(REVISION).down_revision == PREVIOUS
    assert scripts.get_revision(HEAD) is not None


FROZEN_ALIASES = {   # the aliases of the default formats of revision 032 that belong to one format only
    "image/pjpeg": "image/jpeg",
    "video/x-m4v": "video/mp4",
    "video/x-matroska": "video/matroska",
    "audio/mp3": "audio/mpeg",
    "audio/x-wav": "audio/wav", "audio/wave": "audio/wav", "audio/vnd.wave": "audio/wav",
    "audio/x-flac": "audio/flac",
    "audio/opus": "audio/ogg", "application/ogg": "audio/ogg",
    "audio/x-m4a": "audio/mp4", "audio/aac": "audio/mp4",
    "text/rtf": "application/rtf",
}


def test_the_frozen_map():
    """Frozen with the revision: every alias of one default format maps to that format's name; text/plain (an alias
    of Markdown and CSV, and a format of its own) maps through the extension; the seeded renames are the canonical
    names that changed in 032. The defaults may move on, the map never does."""
    assert media_types_v1.ALIASES == FROZEN_ALIASES
    assert media_types_v1.BY_EXTENSION == {
        "text/plain": {".md": "text/markdown", ".markdown": "text/markdown", ".csv": "text/csv"}}
    assert media_types_v1.SEEDED_RENAMES == {"video/x-matroska": "video/matroska"}
    assert media_types_v1.EXTENSIONS == {
        "image/jpeg": (".jpg", ".jpeg"), "video/mp4": (".mp4", ".m4v"), "video/matroska": (".mkv",),
        "audio/mpeg": (".mp3",), "audio/wav": (".wav",), "audio/flac": (".flac",),
        "audio/ogg": (".ogg", ".oga", ".opus"), "audio/mp4": (".m4a",), "application/rtf": (".rtf",)}
    assert {name: (old, new) for name, (old, new) in media_types_v1.SEEDED_ALIASES.items()} == {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (["application/zip"], []),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": (["application/zip"], []),
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": (["application/zip"], []),
        "application/vnd.oasis.opendocument.text": (["application/zip"], []),
        "application/msword": (["application/x-ole-storage", "application/CDFV2"], []),
        "text/csv": (["text/plain", "application/csv"], ["text/plain"]),
    }
    # at this revision the map is what the defaults say
    defaults = {spec.content_type: spec for spec in formats.default_formats()}
    for alias, canonical in FROZEN_ALIASES.items():
        assert alias in defaults[canonical].aliases, alias
        assert formats.canonical_content_type(alias) == canonical, alias
        assert defaults[canonical].extensions == media_types_v1.EXTENSIONS[canonical], canonical
    for name, (_, aliases) in media_types_v1.SEEDED_ALIASES.items():
        assert list(defaults[name].aliases) == aliases, name
    for old, new in media_types_v1.SEEDED_RENAMES.items():
        assert old in defaults[new].aliases and old not in defaults


def test_migration_makes_stored_types_canonical(tmp_path):
    db_file = _database_at_031(tmp_path, SEEDED_AT_031)
    up = run_migrate(db_file)
    assert up.returncode == 0, up.stdout + up.stderr
    assert _media(db_file) == {row_id: after for row_id, (_, after) in MEDIA.items()}
    assert _sessions(db_file) == {session_id: after for session_id, (_, after) in SESSIONS.items()}
    assert _formats(db_file) == {
        1: ("image/jpeg", ["image/pjpeg"]),
        2: ("video/mp4", ["video/x-m4v"]),
        3: ("video/matroska", ["video/x-matroska", "video/x-mkv"]),     # the old name becomes the first alias
        4: ("audio/wav", ["audio/x-wav", "audio/wave", "audio/vnd.wave"]),
        5: ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", []),
        6: ("application/msword", ["application/x-ole-storage"]),
        7: ("text/csv", ["text/plain"]),
    }

    # downgrade is a no-op: canonical names stay valid aliases-wise for the code of 031
    down = _run_downgrade(db_file, PREVIOUS)
    assert down.returncode == 0, down.stdout + down.stderr
    assert _media(db_file) == {row_id: after for row_id, (_, after) in MEDIA.items()}
    assert _formats(db_file)[3][0] == "video/matroska"


def test_an_administrators_own_value_stays(tmp_path):
    """Only a row still holding the old seeded value is renamed: an admin's renamed row stays, and so does a seeded
    row whose new name another row already holds (content_type is unique)"""
    db_file = _database_at_031(tmp_path, [
        (1, "video", "video/x-mkv", [".mkv"], ["video/x-matroska", "video/matroska"]),
        (2, "video", "video/x-matroska", [".mk3d"], []),
        (3, "video", "video/matroska", [".mka2"], []),
    ])
    up = run_migrate(db_file)
    assert up.returncode == 0, up.stdout + up.stderr
    assert _formats(db_file) == {
        1: ("video/x-mkv", ["video/x-matroska", "video/matroska"]),
        2: ("video/x-matroska", []),
        3: ("video/matroska", []),
    }
    assert "video/matroska already taken" in up.stdout + up.stderr


def test_migration_is_idempotent(tmp_path):
    db_file = _database_at_031(tmp_path, SEEDED_AT_031)
    assert run_migrate(db_file).returncode == 0
    conn = sqlite3.connect(db_file)
    _insert_media(conn, 40, "video/x-matroska", "video/x-matroska", ".mkv")     # written by the old code meanwhile
    conn.execute("update alembic_version set version_num = ?", (PREVIOUS,))
    conn.commit()
    conn.close()
    before = _media(db_file)
    formats_before = _formats(db_file)
    again = run_migrate(db_file)
    assert again.returncode == 0, again.stdout + again.stderr
    after = _media(db_file)
    assert after[40] == ("video/matroska", "video/matroska")
    assert {k: v for k, v in after.items() if k != 40} == {k: v for k, v in before.items() if k != 40}
    assert _formats(db_file) == formats_before


def _database_with_media(tmp_path: Path, rows) -> Path:
    """A database at 031 with only the given media rows (id, mime_type, playback_type, extension)"""
    db_file = tmp_path / "media_types_only.db"
    assert run_migrate(db_file).returncode == 0
    assert _run_downgrade(db_file, PREVIOUS).returncode == 0
    conn = sqlite3.connect(db_file)
    for row in rows:
        _insert_media(conn, *row)
    conn.commit()
    conn.close()
    return db_file


def test_only_the_aliases_that_occur_are_looped_over(tmp_path):
    """One read of the distinct values picks the aliases: the log names exactly those found (mime_type, and the base
    of playback_type), every other alias of the map gets no statement"""
    db_file = _database_at_031(tmp_path, SEEDED_AT_031)
    up = run_migrate(db_file)
    assert up.returncode == 0, up.stdout + up.stderr
    out = up.stdout + up.stderr
    assert ("media_files aliases found: mime_type ['application/ogg', 'audio/mp3', 'audio/x-m4a', 'audio/x-wav', "
            "'image/pjpeg', 'video/x-m4v', 'video/x-matroska', 'text/plain'], playback_type ['application/ogg', "
            "'audio/mp3', 'audio/x-m4a', 'audio/x-wav', 'video/x-m4v', 'video/x-matroska']") in out


def test_an_alias_only_in_playback_type_is_made_canonical(tmp_path):
    """A row whose mime_type is canonical but whose playback_type still starts with the alias"""
    db_file = _database_with_media(tmp_path, [(1, "video/matroska", 'video/x-matroska; codecs="vp8"', ".mkv"),
                                              (2, "video/mp4", "video/mp4", ".mp4")])
    up = run_migrate(db_file)
    assert up.returncode == 0, up.stdout + up.stderr
    assert "mime_type [], playback_type ['video/x-matroska']" in up.stdout + up.stderr
    assert _media(db_file) == {1: ("video/matroska", 'video/matroska; codecs="vp8"'), 2: ("video/mp4", "video/mp4")}


def test_no_alias_found_runs_no_batch(tmp_path):
    db_file = _database_with_media(tmp_path, [(1, "video/mp4", 'video/mp4; codecs="avc1"', ".mp4"),
                                              (2, "image/png", None, ".png")])
    up = run_migrate(db_file)
    assert up.returncode == 0, up.stdout + up.stderr
    out = up.stdout + up.stderr
    assert "media_files aliases found: mime_type [], playback_type []" in out and "batches" not in out
    assert _media(db_file) == {1: ("video/mp4", 'video/mp4; codecs="avc1"'), 2: ("image/png", None)}
