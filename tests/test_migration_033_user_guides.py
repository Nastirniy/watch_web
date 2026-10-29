"""033_user_guides: the tables user_guides and user_guide_files (listed in TABLES_OWNED_BY_LATER_REVISIONS of
002_sync_schema, so 002 never builds them), their indexes and the one foreign key user_guide_files.guide_id ->
user_guides.id. Every create is guarded with an inspector check: where the tables exist already (a database built
with create_all, or a second run) 033 changes nothing. Downgrade drops both tables."""
import sqlite3
from pathlib import Path

import pytest
from alembic.script import ScriptDirectory

from app.backend.models import UserGuide, UserGuideFile
from tests.test_column_bounds import _run_downgrade
from tests.test_migrations import HEAD, PROJECT_ROOT, run_migrate

pytestmark = pytest.mark.slow

REVISION = "033_user_guides"
PREVIOUS = "032_canonical_media_types"
TABLES = {"user_guides", "user_guide_files"}
STAMP = "2026-10-29 10:00:00"


def _tables(db_file: Path) -> set:
    conn = sqlite3.connect(db_file)
    names = {row[0] for row in conn.execute("select name from sqlite_master where type = 'table'")}
    conn.close()
    return names


def _indexes(db_file: Path, table: str) -> set:
    """(name, columns) of the named indexes of the table (sqlite's own autoindexes left out)"""
    conn = sqlite3.connect(db_file)
    found = set()
    for row in conn.execute(f"pragma index_list({table})"):
        name = row[1]
        if name.startswith("sqlite_autoindex"):
            continue
        columns = tuple(info[2] for info in conn.execute(f"pragma index_info({name})"))
        found.add((name, columns))
    conn.close()
    return found


def _foreign_keys(db_file: Path, table: str) -> set:
    conn = sqlite3.connect(db_file)
    keys = {(row[3], row[2], row[4]) for row in conn.execute(f"pragma foreign_key_list({table})")}
    conn.close()
    return keys


def _model_indexes(model) -> set:
    return {(index.name, tuple(column.name for column in index.columns)) for index in model.__table__.indexes}


def _version(db_file: Path) -> str:
    conn = sqlite3.connect(db_file)
    value = conn.execute("select version_num from alembic_version").fetchone()[0]
    conn.close()
    return value


def _insert_guide_with_file(db_file: Path) -> None:
    conn = sqlite3.connect(db_file)
    conn.execute("insert into user_guides (id, title, sort_order, is_deleted, created_at, updated_at)"
                 " values (1, 'Getting started', 0, 0, ?, ?)", (STAMP, STAMP))
    conn.execute("insert into user_guide_files (id, guide_id, file_name, stored_name, file_path, content_type,"
                 " format_id, size_bytes, sha256, is_deleted, created_at)"
                 " values (1, 1, 'start.pdf', 's.pdf', '_guides/1/1/s.pdf', 'application/pdf', 1, 10, ?, 0, ?)",
                 ("ab" * 32, STAMP))
    conn.commit()
    conn.close()


def test_033_is_in_the_chain():
    scripts = ScriptDirectory(str(PROJECT_ROOT / "app" / "backend" / "migrations"))
    assert scripts.get_revision(REVISION).down_revision == PREVIOUS
    assert HEAD == REVISION


def test_002_leaves_the_tables_to_033():
    import importlib
    sync = importlib.import_module("app.backend.migrations.versions.002_sync_schema_with_models")
    assert TABLES <= sync.TABLES_OWNED_BY_LATER_REVISIONS


def test_fresh_install_gets_the_tables_from_033_and_down_drops_them(tmp_path):
    """A fresh install: 002 skips the two tables, 033 creates them with the indexes and the foreign key of the
    models; the downgrade drops them and an upgrade creates them again"""
    db_file = tmp_path / "guides.db"
    first = run_migrate(db_file)
    assert first.returncode == 0, first.stdout + first.stderr
    assert _version(db_file) == HEAD
    assert TABLES <= _tables(db_file)
    assert "creating missing table user_guide" not in first.stdout + first.stderr   # not built by 002
    assert _indexes(db_file, "user_guides") == _model_indexes(UserGuide)
    assert _indexes(db_file, "user_guide_files") == _model_indexes(UserGuideFile)
    assert _foreign_keys(db_file, "user_guide_files") == {("guide_id", "user_guides", "id")}
    assert _foreign_keys(db_file, "user_guides") == set()
    _insert_guide_with_file(db_file)

    down = _run_downgrade(db_file, PREVIOUS)
    assert down.returncode == 0, down.stdout + down.stderr
    assert not TABLES & _tables(db_file)
    assert _version(db_file) == PREVIOUS

    again = run_migrate(db_file)
    assert again.returncode == 0, again.stdout + again.stderr
    assert TABLES <= _tables(db_file)
    assert _indexes(db_file, "user_guide_files") == _model_indexes(UserGuideFile)
    conn = sqlite3.connect(db_file)
    assert conn.execute("select count(*) from user_guides").fetchone()[0] == 0
    conn.close()


def test_033_is_a_no_op_where_the_tables_exist(tmp_path):
    """A second run of 033 (the alembic version put back to 032) over the tables it created keeps them and their
    rows"""
    db_file = tmp_path / "guides.db"
    first = run_migrate(db_file)
    assert first.returncode == 0, first.stdout + first.stderr
    _insert_guide_with_file(db_file)
    conn = sqlite3.connect(db_file)
    conn.execute("update alembic_version set version_num = ?", (PREVIOUS,))
    conn.commit()
    conn.close()

    again = run_migrate(db_file)
    assert again.returncode == 0, again.stdout + again.stderr
    assert _version(db_file) == HEAD
    conn = sqlite3.connect(db_file)
    assert conn.execute("select id, title from user_guides").fetchall() == [(1, "Getting started")]
    assert conn.execute("select id, guide_id from user_guide_files").fetchall() == [(1, 1)]
    conn.close()
    assert _indexes(db_file, "user_guides") == _model_indexes(UserGuide)
    assert _indexes(db_file, "user_guide_files") == _model_indexes(UserGuideFile)
