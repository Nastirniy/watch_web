"""037_outbox_events: the table of events to the web client, with its primary key as its only index.

A fresh install gets it from 037 (listed in TABLES_OWNED_BY_LATER_REVISIONS); the create is guarded, so a second run
leaves the table and its rows as they are.
"""
import sqlite3
from pathlib import Path

from alembic.script import ScriptDirectory

from tests.test_column_bounds import _run_downgrade
from tests.test_migrations import HEAD, PROJECT_ROOT, run_migrate

REVISION = "037_outbox_events"
PREVIOUS = "036_webm_media_types"


def tables(db_file: Path) -> set:
    conn = sqlite3.connect(db_file)
    names = {row[0] for row in conn.execute("select name from sqlite_master where type = 'table'")}
    conn.close()
    return names


def test_037_is_in_the_chain():
    scripts = ScriptDirectory(str(PROJECT_ROOT / "app" / "backend" / "migrations"))
    assert scripts.get_revision(REVISION).down_revision == PREVIOUS
    assert HEAD == REVISION


def test_fresh_install_down_and_up_again(tmp_path):
    db_file = tmp_path / "events.db"
    first = run_migrate(db_file)
    assert first.returncode == 0, first.stdout + first.stderr
    assert "outbox_events" in tables(db_file)
    conn = sqlite3.connect(db_file)
    columns = {row[1] for row in conn.execute("pragma table_info(outbox_events)")}
    assert columns == {"id", "type", "v", "scope_key", "permission", "user_id", "conflate_key", "payload",
                       "occurred_at"}
    assert [row for row in conn.execute("pragma index_list(outbox_events)") if row[3] != "pk"] == []
    conn.execute("insert into outbox_events (type, v, scope_key, permission, payload) "
                 "values ('scope.changed', 1, 'g:2', 'post.view', '{}')")
    conn.commit()
    assert conn.execute("select occurred_at from outbox_events").fetchone()[0], "the database clock fills it"
    conn.close()

    down = _run_downgrade(db_file, PREVIOUS)
    assert down.returncode == 0, down.stdout + down.stderr
    assert "outbox_events" not in tables(db_file)
    assert "delivery data" in down.stdout + down.stderr

    # a table that exists already (a database built with create_all) is left as it is
    conn = sqlite3.connect(db_file)
    conn.execute("create table outbox_events (id integer primary key, type varchar(48) not null, v integer not null, "
                 "scope_key varchar(24), permission varchar(48), user_id integer, conflate_key varchar(48), "
                 "payload varchar(512) not null, occurred_at datetime not null default current_timestamp)")
    conn.execute("insert into outbox_events (type, v, payload) values ('scope.changed', 1, '{}')")
    conn.commit()
    conn.close()
    up = run_migrate(db_file)
    assert up.returncode == 0, up.stdout + up.stderr
    conn = sqlite3.connect(db_file)
    assert conn.execute("select count(*) from outbox_events").fetchone() == (1,)
    conn.close()
