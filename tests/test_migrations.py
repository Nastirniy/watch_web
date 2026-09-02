"""Alembic chain on an empty sqlite database: 002_sync_schema creates the schema and is idempotent."""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def run_migrate(db_file: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["WATCH_WEB_ALEMBIC_URL"] = f"sqlite+aiosqlite:///{db_file.as_posix()}"
    return subprocess.run(
        [sys.executable, "-m", "app.backend.migrations_manager", "migrate"],
        cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=300,
    )


@pytest.mark.slow
def test_migration_chain_on_empty_database(tmp_path):
    db_file = tmp_path / "migrations.db"

    first = run_migrate(db_file)
    assert first.returncode == 0, first.stdout + first.stderr
    assert "creating missing table" in first.stdout + first.stderr

    conn = sqlite3.connect(db_file)
    tables = {row[0] for row in conn.execute("select name from sqlite_master where type='table'")}
    assert {"users", "roles", "permissions", "observations", "media_files", "user_post_access"} <= tables
    assert conn.execute("select version_num from alembic_version").fetchone()[0] == "004_role_soft_delete"

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
def test_migration_head_is_004(tmp_path):
    db_file = tmp_path / "head.db"
    result = run_migrate(db_file)
    assert result.returncode == 0, result.stdout + result.stderr
    conn = sqlite3.connect(db_file)
    assert conn.execute("select version_num from alembic_version").fetchone()[0] == "004_role_soft_delete"
    columns = {row[1] for row in conn.execute("pragma table_info(roles)")}
    assert "is_deleted" in columns
    unique_indexes = [row for row in conn.execute("pragma index_list(user_roles)") if row[2] == 1]
    assert unique_indexes, "user_roles must have a unique index"
    conn.close()
