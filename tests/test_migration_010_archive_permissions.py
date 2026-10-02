"""010_archive_permissions: X.delete rows become X.archive in place, role grants survive, role.delete is not kept."""
import sqlite3

import pytest

from tests.test_migrations import run_migrate, HEAD


def _rewind(conn: sqlite3.Connection, revision: str) -> None:
    conn.execute("delete from alembic_version")
    conn.execute("insert into alembic_version values (?)", (revision,))
    conn.commit()


@pytest.mark.slow
def test_rename_keeps_grants_and_merges_duplicates(tmp_path):
    db_file = tmp_path / "perms.db"
    first = run_migrate(db_file)
    assert first.returncode == 0, first.stdout + first.stderr

    conn = sqlite3.connect(db_file)
    # A database as it looked before this revision: old names, one role holding them
    conn.executemany(
        "insert into permissions (id, name, display_name, description) values (?, ?, ?, ?)",
        [
            (1, "role.delete", "old", ""),
            (2, "tag.delete", "old", ""),
            (3, "tag.view", "", ""),
            # the app was started on the new code once: tag.archive already exists with its own grant
            (4, "tag.archive", "new", ""),
        ],
    )
    conn.execute("insert into roles (id, name, display_name, created_at, updated_at, is_deleted) values (10, 'ops', 'Ops', '2026-01-01', '2026-01-01', 0)")
    conn.execute("insert into roles (id, name, display_name, created_at, updated_at, is_deleted) values (11, 'other', 'Other', '2026-01-01', '2026-01-01', 0)")
    conn.executemany(
        "insert into role_permissions (role_id, permission_id) values (?, ?)",
        [(10, 1), (10, 2), (10, 3), (11, 2), (11, 4)],
    )
    _rewind(conn, "009_media_playback_metadata")
    conn.close()

    second = run_migrate(db_file)
    assert second.returncode == 0, second.stdout + second.stderr
    out = second.stdout + second.stderr
    assert "renaming permission role.delete -> role.archive" in out
    assert "merging permission tag.delete (id 2) into tag.archive (id 4)" in out

    conn = sqlite3.connect(db_file)
    names = dict(conn.execute("select id, name from permissions").fetchall())
    assert names == {1: "role.archive", 3: "tag.view", 4: "tag.archive"}, "old role.delete row renamed, tag.delete merged away"
    grants = {(r, p) for r, p in conn.execute("select role_id, permission_id from role_permissions").fetchall()}
    assert grants == {(10, 1), (10, 3), (10, 4), (11, 4)}, "ops keeps role.archive and tag.archive, other keeps tag.archive once"
    assert conn.execute("select version_num from alembic_version").fetchone()[0] == HEAD, "chain continues to the current head"
    conn.close()

    third = run_migrate(db_file)
    assert third.returncode == 0 and "renaming permission" not in third.stdout + third.stderr
