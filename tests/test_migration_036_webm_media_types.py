"""036_webm_media_types: media_files rows of .webm files holding video/x-matroska (032 left them: not its Matroska
format) become video/webm, with the base of playback_type, through the frozen map of
migrations/support/media_types_v2.py, in batches of 10,000 ids. Every other row keeps its data; downgrade changes
nothing."""
import sqlite3
from pathlib import Path

import pytest
from alembic.script import ScriptDirectory

from app.backend.enums.media_types import ContentType
from app.backend.migrations.support import media_types_v2
from app.backend.services.files import formats
from tests.test_column_bounds import _run_downgrade
from tests.test_migrations import HEAD, PROJECT_ROOT, run_migrate

pytestmark = pytest.mark.slow

REVISION = "036_webm_media_types"
PREVIOUS = "035_queued_moves"
STAMP = "2026-10-01 10:00:00"
FAR_ID = 25_003     # a row two batches away from the others

# id: (mime_type, playback_type, file_extension) -> (mime_type, playback_type) after 036
MEDIA = {
    1: (("video/x-matroska", 'video/x-matroska; codecs="vp9, opus"', ".webm"), ("video/webm", 'video/webm; codecs="vp9, opus"')),
    2: (("video/x-matroska", "video/x-matroska", ".webm"), ("video/webm", "video/webm")),
    3: (("video/x-matroska", None, ".webm"), ("video/webm", None)),
    4: (("video/x-matroska", 'video/x-matroska; codecs="vp8"', "webm"), ("video/webm", 'video/webm; codecs="vp8"')),     # no dot
    5: (("video/x-matroska", None, ".WEBM"), ("video/webm", None)),                                  # upper case extension
    # a canonical type with a playback_type that still starts with the old name
    6: (("video/webm", 'video/x-matroska; codecs="vp9"', ".webm"), ("video/webm", 'video/webm; codecs="vp9"')),
    # other files keep everything
    7: (("video/x-matroska", 'video/x-matroska; codecs="vp9"', ".mkv"), ("video/x-matroska", 'video/x-matroska; codecs="vp9"')),
    8: (("video/matroska", 'video/matroska; codecs="avc1"', ".mkv"), ("video/matroska", 'video/matroska; codecs="avc1"')),
    9: (("video/webm", 'video/webm; codecs="vp9"', ".webm"), ("video/webm", 'video/webm; codecs="vp9"')),
    10: (("video/mp4", 'video/mp4; codecs="avc1"', ".mp4"), ("video/mp4", 'video/mp4; codecs="avc1"')),
    11: (("video/x-matroska-3d", "video/x-matroska-3d", ".webm"), ("video/x-matroska-3d", "video/x-matroska-3d")),     # not a prefix match
    FAR_ID: (("video/x-matroska", 'video/x-matroska; codecs="vp9"', ".webm"), ("video/webm", 'video/webm; codecs="vp9"')),
}


def _insert_media(conn, row_id, mime_type, playback_type, extension):
    conn.execute("insert into media_files (id, observation_id, original_filename, stored_filename, file_path,"
                 " file_size, mime_type, file_extension, media_type, file_hash, poster_attempts, uploaded_at,"
                 " is_deleted, playback_type) values (?, 1, ?, ?, ?, 1, ?, ?, 'video', 'h', 0, ?, 0, ?)",
                 (row_id, f"a{extension}", f"{row_id}{extension}", f"{row_id}{extension}", mime_type, extension,
                  STAMP, playback_type))


def _database_at_035(tmp_path: Path) -> Path:
    db_file = tmp_path / "webm.db"
    first = run_migrate(db_file)
    assert first.returncode == 0, first.stdout + first.stderr
    down = _run_downgrade(db_file, PREVIOUS)
    assert down.returncode == 0, down.stdout + down.stderr
    conn = sqlite3.connect(db_file)
    for row_id, ((mime_type, playback_type, extension), _) in MEDIA.items():
        _insert_media(conn, row_id, mime_type, playback_type, extension)
    conn.commit()
    conn.close()
    return db_file


def _media(db_file: Path) -> dict:
    conn = sqlite3.connect(db_file)
    rows = {row[0]: (row[1], row[2]) for row in conn.execute("select id, mime_type, playback_type from media_files")}
    conn.close()
    return rows


def test_036_is_in_the_chain():
    scripts = ScriptDirectory(str(PROJECT_ROOT / "app" / "backend" / "migrations"))
    assert scripts.get_revision(REVISION).down_revision == PREVIOUS
    assert scripts.get_revision(HEAD) is not None


def test_the_frozen_map():
    """Frozen with the revision: the defaults may move on, the map never does. At this revision it is what they say:
    .webm is the WebM format, video/x-matroska an alias of another one"""
    assert media_types_v2.WEBM_EXTENSIONS == (".webm",)
    assert media_types_v2.OLD_NAME == "video/x-matroska"
    assert media_types_v2.WEBM == "video/webm"
    defaults = {spec.content_type: spec for spec in formats.default_formats()}
    assert defaults[ContentType.VIDEO_WEBM].extensions == media_types_v2.WEBM_EXTENSIONS
    assert ContentType.VIDEO_X_MATROSKA.value == media_types_v2.OLD_NAME
    assert ContentType.VIDEO_WEBM.value == media_types_v2.WEBM


def test_migration_rewrites_webm_rows_and_keeps_every_other_row(tmp_path):
    db_file = _database_at_035(tmp_path)
    up = run_migrate(db_file)
    assert up.returncode == 0, up.stdout + up.stderr
    assert _media(db_file) == {row_id: after for row_id, (_, after) in MEDIA.items()}
    assert "in 3 batches" in up.stdout + up.stderr

    # downgrade is a no-op: video/webm is a valid name for the code of 035, and a row cannot tell what it held
    down = _run_downgrade(db_file, PREVIOUS)
    assert down.returncode == 0, down.stdout + down.stderr
    assert _media(db_file) == {row_id: after for row_id, (_, after) in MEDIA.items()}


def test_migration_is_idempotent(tmp_path):
    db_file = _database_at_035(tmp_path)
    assert run_migrate(db_file).returncode == 0
    conn = sqlite3.connect(db_file)
    _insert_media(conn, 40, "video/x-matroska", "video/x-matroska", ".webm")     # written by the old code meanwhile
    conn.execute("update alembic_version set version_num = ?", (PREVIOUS,))
    conn.commit()
    conn.close()
    before = _media(db_file)
    again = run_migrate(db_file)
    assert again.returncode == 0, again.stdout + again.stderr
    after = _media(db_file)
    assert after[40] == ("video/webm", "video/webm")
    assert {k: v for k, v in after.items() if k != 40} == {k: v for k, v in before.items() if k != 40}


def test_no_row_found_runs_no_batch(tmp_path):
    db_file = tmp_path / "webm_none.db"
    assert run_migrate(db_file).returncode == 0
    assert _run_downgrade(db_file, PREVIOUS).returncode == 0
    conn = sqlite3.connect(db_file)
    _insert_media(conn, 1, "video/mp4", 'video/mp4; codecs="avc1"', ".mp4")
    conn.commit()
    conn.close()
    up = run_migrate(db_file)
    assert up.returncode == 0, up.stdout + up.stderr
    out = up.stdout + up.stderr
    assert "no row holds video/x-matroska" in out and "batches" not in out
    assert _media(db_file) == {1: ("video/mp4", 'video/mp4; codecs="avc1"')}
