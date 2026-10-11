"""Alembic chain on an empty sqlite database: 002_sync_schema creates the schema and is idempotent."""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HEAD = "018_media_file_size_bigint"


def run_migrate(db_file: Path, cwd: Path = PROJECT_ROOT) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["WATCH_WEB_ALEMBIC_URL"] = f"sqlite+aiosqlite:///{db_file.as_posix()}"
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    return subprocess.run(
        [sys.executable, "-m", "app.backend.cli.migrations_manager", "migrate"],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=300,
    )


@pytest.mark.slow
def test_migrate_from_another_directory_over_create_all_tables(tmp_path):
    """The IDE starts the app in app/. alembic.ini resolved script_location against the working directory, the
    startup swallowed the failure and built the tables with create_all, without alembic_version. Paths are now
    relative to alembic.ini (%(here)s, path_separator = os), and such a database upgrades to head."""
    from sqlalchemy import create_engine
    from sqlmodel import SQLModel
    import app.backend.models  # noqa: F401  (registers every table)

    db_file = tmp_path / "create_all.db"
    engine = create_engine(f"sqlite:///{db_file.as_posix()}")
    SQLModel.metadata.create_all(engine)
    engine.dispose()

    result = run_migrate(db_file, cwd=PROJECT_ROOT / "app")
    assert result.returncode == 0, result.stdout + result.stderr
    conn = sqlite3.connect(db_file)
    assert conn.execute("select version_num from alembic_version").fetchone()[0] == HEAD
    conn.close()


@pytest.mark.slow
def test_migrations_at_startup_keep_the_server_loggers(tmp_path):
    """The app runs Alembic in its own process at startup (bootstrap.startup._migrate_and_seed). env.py configured logging
    from alembic.ini with fileConfig's default disable_existing_loggers=True, which turned off uvicorn's loggers:
    the console stopped at the last migration, no 'Uvicorn running on ...' and no access log, the server looked hung."""
    env = dict(os.environ)
    env["WATCH_WEB_ALEMBIC_URL"] = f"sqlite+aiosqlite:///{(tmp_path / 'startup.db').as_posix()}"
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    script = (
        "import logging\n"
        "from alembic import command\n"
        "from alembic.config import Config\n"
        "names = ('uvicorn', 'uvicorn.error', 'uvicorn.access', 'fastapi')\n"
        "for name in names:\n"
        "    logging.getLogger(name)\n"
        f"command.upgrade(Config({str(PROJECT_ROOT / 'alembic.ini')!r}), 'head')\n"
        "print('DISABLED=' + ','.join(n for n in names if logging.getLogger(n).disabled))\n"
    )
    result = subprocess.run([sys.executable, "-c", script], cwd=PROJECT_ROOT, env=env,
                            capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "DISABLED=\n" in result.stdout, result.stdout


@pytest.mark.slow
def test_migration_chain_on_empty_database(tmp_path):
    db_file = tmp_path / "migrations.db"

    first = run_migrate(db_file)
    assert first.returncode == 0, first.stdout + first.stderr
    assert "creating missing table" in first.stdout + first.stderr

    conn = sqlite3.connect(db_file)
    tables = {row[0] for row in conn.execute("select name from sqlite_master where type='table'")}
    assert {"users", "roles", "permissions", "observations", "media_files", "user_post_access", "worker_heartbeats", "media_formats", "upload_sessions", "observation_frequencies", "video_storyboards"} <= tables
    assert conn.execute("select version_num from alembic_version").fetchone()[0] == HEAD
    media_columns = {row[1] for row in conn.execute("pragma table_info(media_files)")}
    assert {"poster_status", "poster_attempts", "poster_locked_until", "poster_worker", "poster_generated_at", "poster_error"} <= media_columns
    assert "thumbnail_path" not in media_columns and "thumbnail_error" not in media_columns
    assert {"duration_seconds", "video_codec", "audio_codec", "playback_type"} <= media_columns

    second = run_migrate(db_file)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "creating missing" not in second.stdout + second.stderr
    assert "adding missing column" not in second.stdout + second.stderr

    # Simulate a production database lacking a column: rewind to 001 and let 002 add it back
    conn.execute("alter table tags drop column is_deleted")
    conn.execute("delete from alembic_version")
    conn.execute("insert into alembic_version values ('001_relativize_media_paths')")
    conn.commit()
    conn.close()

    third = run_migrate(db_file)
    assert third.returncode == 0, third.stdout + third.stderr
    assert "adding missing column tags.is_deleted" in third.stdout + third.stderr
    conn = sqlite3.connect(db_file)
    columns = {row[1] for row in conn.execute("pragma table_info(tags)")}
    assert "is_deleted" in columns
    conn.close()


@pytest.mark.slow
def test_005_and_006_upgrade_a_legacy_database(tmp_path):
    """A database at 004 with the old thumbnail columns: 005 queues videos, 006 drops the columns."""
    db_file = tmp_path / "legacy.db"
    first = run_migrate(db_file)
    assert first.returncode == 0, first.stdout + first.stderr

    conn = sqlite3.connect(db_file)
    conn.execute("alter table media_files add column thumbnail_path varchar(500)")
    conn.execute("alter table media_files add column thumbnail_error varchar(500)")
    conn.execute("drop index if exists ix_media_files_poster_status")
    for name in ("poster_status", "poster_attempts", "poster_locked_until", "poster_worker", "poster_generated_at", "poster_error"):
        conn.execute(f"alter table media_files drop column {name}")
    conn.execute("drop table worker_heartbeats")
    conn.execute(
        "insert into media_files (original_filename, stored_filename, file_path, file_size, mime_type, file_extension, media_type, file_hash, uploaded_at, is_deleted, thumbnail_path)"
        " values ('a.mp4', 's.mp4', '2026/09/01/obs_001/s.mp4', 10, 'video/mp4', 'mp4', 'video', 'h', '2026-09-01 00:00:00', 0, '2026/09/01/obs_001/s_thumb_medium.jpg')"
    )
    conn.execute(
        "insert into media_files (original_filename, stored_filename, file_path, file_size, mime_type, file_extension, media_type, file_hash, uploaded_at, is_deleted)"
        " values ('b.jpg', 't.jpg', '2026/09/01/obs_001/t.jpg', 10, 'image/jpeg', 'jpg', 'image', 'i', '2026-09-01 00:00:00', 0)"
    )
    conn.execute("delete from alembic_version")
    conn.execute("insert into alembic_version values ('004_role_soft_delete')")
    conn.commit()
    conn.close()

    second = run_migrate(db_file)
    assert second.returncode == 0, second.stdout + second.stderr
    output = second.stdout + second.stderr
    assert "queued 1 existing video(s)" in output
    assert "dropping media_files.thumbnail_path" in output

    conn = sqlite3.connect(db_file)
    rows = conn.execute("select media_type, poster_status from media_files order by id").fetchall()
    assert rows == [("video", "pending"), ("image", None)]
    columns = {row[1] for row in conn.execute("pragma table_info(media_files)")}
    assert "thumbnail_path" not in columns
    assert "worker_heartbeats" in {row[0] for row in conn.execute("select name from sqlite_master where type='table'")}
    conn.close()


def _load_004():
    import importlib.util
    path = PROJECT_ROOT / "app" / "backend" / "migrations" / "versions" / "004_role_soft_delete_and_unique_assignments.py"
    spec = importlib.util.spec_from_file_location("migration_004", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_004_duplicate_removal_and_unique_detection(tmp_path):
    import sqlalchemy as sa
    module = _load_004()
    engine = sa.create_engine(f"sqlite:///{(tmp_path / 'dedupe.db').as_posix()}")
    with engine.begin() as conn:
        conn.execute(sa.text("create table user_roles (id integer primary key, user_id integer, role_id integer, assigned_at datetime)"))
        conn.execute(sa.text("insert into user_roles (user_id, role_id) values (1, 1), (1, 1), (2, 1), (1, 1), (2, 2)"))
        removed = module._remove_duplicates(conn, "user_roles", ("user_id", "role_id"))
        assert removed == 2
        rows = conn.execute(sa.text("select user_id, role_id from user_roles order by id")).fetchall()
        assert [tuple(r) for r in rows] == [(1, 1), (2, 1), (2, 2)]
        assert not module._has_unique(sa.inspect(conn), "user_roles", ("user_id", "role_id"))
        conn.execute(sa.text("create unique index uq_user_roles_user_role on user_roles (user_id, role_id)"))
        assert module._has_unique(sa.inspect(conn), "user_roles", ("user_id", "role_id"))


@pytest.mark.slow
def test_migration_head(tmp_path):
    db_file = tmp_path / "head.db"
    result = run_migrate(db_file)
    assert result.returncode == 0, result.stdout + result.stderr
    conn = sqlite3.connect(db_file)
    assert conn.execute("select version_num from alembic_version").fetchone()[0] == HEAD
    columns = {row[1] for row in conn.execute("pragma table_info(roles)")}
    assert "is_deleted" in columns
    unique_indexes = [row for row in conn.execute("pragma index_list(user_roles)") if row[2] == 1]
    assert unique_indexes, "user_roles must have a unique index"
    conn.close()


@pytest.mark.slow
def test_007_decodes_html_entities_in_text_columns(tmp_path):
    """Rows escaped by the old html.escape input filter come out as plain text; unique clashes are skipped."""
    db_file = tmp_path / "entities.db"
    first = run_migrate(db_file)
    assert first.returncode == 0, first.stdout + first.stderr

    conn = sqlite3.connect(db_file)
    conn.execute("insert into posts (name, code, description, is_active, created_at, updated_at)"
                 " values ('Пост &#x27;Північ&#x27;', 'P&amp;1', 'AT&amp;T &lt;b&gt;', 1, '2026-09-01', '2026-09-01')")
    conn.execute("insert into tags (name, created_at, is_deleted) values ('кур&#x27;єр', '2026-09-01', 0)")
    conn.execute("insert into tags (name, created_at, is_deleted) values ('a&amp;b', '2026-09-01', 0)")
    conn.execute("insert into tags (name, created_at, is_deleted) values ('a&b', '2026-09-01', 0)")
    conn.execute("delete from alembic_version")
    conn.execute("insert into alembic_version values ('006_drop_legacy_thumbnails')")
    conn.commit()
    conn.close()

    second = run_migrate(db_file)
    assert second.returncode == 0, second.stdout + second.stderr
    output = second.stdout + second.stderr
    assert "decoded 1 value(s) in tags.name" in output
    assert "skipping tags.name" in output

    conn = sqlite3.connect(db_file)
    assert conn.execute("select name, code, description from posts").fetchone() == ("Пост 'Північ'", "P&1", "AT&T <b>")
    assert [r[0] for r in conn.execute("select name from tags order by id")] == ["кур'єр", "a&amp;b", "a&b"]
    assert conn.execute("select version_num from alembic_version").fetchone()[0] == HEAD
    conn.close()


@pytest.mark.slow
def test_014_drops_post_connection_columns(tmp_path):
    """A database created before 014 still has both columns and their indexes; 014 drops them and keeps the rows."""
    db_file = tmp_path / "connection.db"
    first = run_migrate(db_file)
    assert first.returncode == 0, first.stdout + first.stderr

    conn = sqlite3.connect(db_file)
    conn.execute("alter table posts add column connection_status varchar(20) not null default 'offline'")
    conn.execute("alter table posts add column last_connection_at datetime")
    conn.execute("create index ix_posts_connection_status on posts (connection_status)")
    conn.execute("create index ix_posts_last_connection_at on posts (last_connection_at)")
    conn.execute("insert into posts (name, code, is_active, connection_status, created_at, updated_at)"
                 " values ('Post', 'P1', 1, 'maintenance', '2026-09-01', '2026-09-01')")
    conn.execute("update alembic_version set version_num = '013_observation_count_index'")
    conn.commit()
    conn.close()

    second = run_migrate(db_file)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "dropping column posts.connection_status" in second.stdout + second.stderr

    conn = sqlite3.connect(db_file)
    columns = {row[1] for row in conn.execute("pragma table_info(posts)")}
    assert not {"connection_status", "last_connection_at"} & columns
    indexes = {row[1] for row in conn.execute("pragma index_list(posts)")}
    assert not {"ix_posts_connection_status", "ix_posts_last_connection_at"} & indexes
    assert conn.execute("select code from posts").fetchall() == [("P1",)]
    assert conn.execute("select version_num from alembic_version").fetchone()[0] == HEAD
    conn.close()


def test_015_moves_session_state_from_is_active_to_ended_at(tmp_path):
    """A database from before 015 has user_sessions.is_active; 015 keeps the ended sessions ended (ended_at),
    adds end_reason / revoke_cause and drops is_active with its index."""
    db_file = tmp_path / "sessions.db"
    first = run_migrate(db_file)
    assert first.returncode == 0, first.stdout + first.stderr

    conn = sqlite3.connect(db_file)  # sqlite checks no foreign keys here: the sessions need no user row
    conn.execute("alter table user_sessions add column is_active boolean not null default 1")
    conn.execute("create index ix_user_sessions_is_active on user_sessions (is_active)")
    rows = [("live", 1, None), ("logged-out", 0, "2026-09-02 10:00:00"), ("ended-without-time", 0, None)]
    for session_id, is_active, ended_at in rows:
        conn.execute("insert into user_sessions (id, user_id, is_active, last_activity, expires_at, ended_at, created_at)"
                     " values (?, 1, ?, '2026-09-02 09:00:00', '2026-09-02 21:00:00', ?, '2026-09-02 08:00:00')",
                     (session_id, is_active, ended_at))
    conn.execute("update alembic_version set version_num = '014_drop_post_connection_columns'")
    conn.commit()
    conn.close()

    second = run_migrate(db_file)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "dropping column user_sessions.is_active" in second.stdout + second.stderr

    conn = sqlite3.connect(db_file)
    columns = {row[1] for row in conn.execute("pragma table_info(user_sessions)")}
    assert "is_active" not in columns and {"end_reason", "revoke_cause", "ended_at"} <= columns
    indexes = {row[1] for row in conn.execute("pragma index_list(user_sessions)")}
    assert "ix_user_sessions_is_active" not in indexes and "ix_user_sessions_ended_at" in indexes
    ended_at = dict(conn.execute("select id, ended_at from user_sessions"))
    assert ended_at["live"] is None
    assert ended_at["logged-out"].startswith("2026-09-02 10:00:00")
    assert ended_at["ended-without-time"].startswith("2026-09-02 09:00:00"), "last_activity becomes the end"
    assert conn.execute("select version_num from alembic_version").fetchone()[0] == HEAD
    conn.close()


def test_017_creates_video_storyboards_on_upgrade(tmp_path):
    """A database at 016 has no video_storyboards; 017 creates the table and its claim index."""
    db_file = tmp_path / "storyboards.db"
    first = run_migrate(db_file)
    assert first.returncode == 0, first.stdout + first.stderr

    conn = sqlite3.connect(db_file)
    conn.execute("drop table video_storyboards")
    conn.execute("update alembic_version set version_num = '016_session_certificate_binding'")
    conn.commit()
    conn.close()

    second = run_migrate(db_file)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "creating video_storyboards" in second.stdout + second.stderr

    conn = sqlite3.connect(db_file)
    columns = {row[1] for row in conn.execute("pragma table_info(video_storyboards)")}
    assert {"media_id", "status", "attempts", "locked_until", "worker", "error", "generated_at", "step_seconds",
            "frame_count", "columns", "rows", "tile_width", "tile_height", "sheet_count"} <= columns
    indexes = {row[1] for row in conn.execute("pragma index_list(video_storyboards)")}
    assert "ix_video_storyboards_status_locked" in indexes
    assert conn.execute("select version_num from alembic_version").fetchone()[0] == HEAD
    conn.close()
