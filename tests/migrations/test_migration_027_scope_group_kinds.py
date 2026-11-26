"""027_scope_group_kinds: scope_groups.kind (existing rows are plain groups) and the observation_units table."""
import sqlite3
from pathlib import Path

from alembic.script import ScriptDirectory

from tests.migrations.test_column_bounds import _run_downgrade
from tests.migrations.test_migrations import HEAD, PROJECT_ROOT, run_migrate

REVISION = "027_scope_group_kinds"
PREVIOUS = "026_users_no_hot_columns"


def _columns(db_file: Path, table: str) -> set:
    conn = sqlite3.connect(db_file)
    names = {row[1] for row in conn.execute(f"pragma table_info({table})")}
    conn.close()
    return names


def _tables(db_file: Path) -> set:
    conn = sqlite3.connect(db_file)
    names = {row[0] for row in conn.execute("select name from sqlite_master where type = 'table'")}
    conn.close()
    return names


def test_027_is_in_the_chain():
    """027 sits on 026 and is an ancestor of the head"""
    scripts = ScriptDirectory(str(PROJECT_ROOT / "app" / "backend" / "migrations"))
    assert scripts.get_revision(REVISION).down_revision == PREVIOUS
    assert REVISION in {script.revision for script in scripts.iterate_revisions(HEAD, "base")}


def test_up_down_up_keeps_groups_as_plain_groups(tmp_path):
    db_file = tmp_path / "kinds.db"
    first = run_migrate(db_file)
    assert first.returncode == 0, first.stdout + first.stderr
    assert "kind" in _columns(db_file, "scope_groups")
    assert {"observation_id", "group_id", "created_at"} <= _columns(db_file, "observation_units")

    down = _run_downgrade(db_file, PREVIOUS)
    assert down.returncode == 0, down.stdout + down.stderr
    assert "kind" not in _columns(db_file, "scope_groups")
    assert "observation_units" not in _tables(db_file)

    conn = sqlite3.connect(db_file)
    conn.execute("insert into scope_groups (id, name, parent_key, is_deleted, created_at, updated_at) "
                 "values (5, 'East', 0, 0, '2026-10-20 10:00:00', '2026-10-20 10:00:00')")
    conn.commit()
    conn.close()

    again = run_migrate(db_file)
    assert again.returncode == 0, again.stdout + again.stderr
    conn = sqlite3.connect(db_file)
    assert conn.execute("select kind from scope_groups where id = 5").fetchone() == ("group",)
    conn.close()
    assert "observation_units" in _tables(db_file)
