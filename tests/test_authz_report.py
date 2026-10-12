"""authz_report: the dry run of migration 023 on a database at 022, read-only; the same plan the migration writes
into its audit rows."""
import json
import os
import sqlite3
import subprocess
import sys

import pytest

from tests.test_migration_023_authz import PREVIOUS, USERS, legacy_database, snapshot
from tests.test_migrations import PROJECT_ROOT, run_migrate


def run_report(db_file, *args) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    return subprocess.run(
        [sys.executable, "-m", "app.backend.cli.authz_report", *args, "--url",
         f"sqlite+aiosqlite:///{db_file.as_posix()}"],
        cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=300,
    )


@pytest.mark.slow
def test_report_matches_migration(tmp_path):
    db_file = legacy_database(tmp_path)
    before = snapshot(db_file)

    report = run_report(db_file, "--json")
    assert report.returncode == 0, report.stdout + report.stderr
    planned = json.loads(report.stdout)
    assert planned["revision"] == PREVIOUS
    assert {entry["user_id"] for entry in planned["users"]} == set(USERS)
    assert {role["name"] for role in planned["derived_roles"]} == {"ops_view", "mgr_system", "mgr_posts"}

    # read-only: the database is still at 022 with every legacy row
    assert snapshot(db_file) == before
    conn = sqlite3.connect(db_file)
    assert conn.execute("select version_num from alembic_version").fetchone()[0] == PREVIOUS
    assert "role_assignments" not in {r[0] for r in conn.execute("select name from sqlite_master where type='table'")}
    conn.close()

    text = run_report(db_file)
    assert text.returncode == 0, text.stdout + text.stderr
    assert "oper" in text.stdout and "ops_view @ p:9" in text.stdout and "derived roles" in text.stdout

    assert run_migrate(db_file).returncode == 0
    conn = sqlite3.connect(db_file)
    audit = {user_id: json.loads(details) for user_id, details in conn.execute(
        "select resource_id, details from audit_logs where action = 'migrate' and resource_type = 'user'")}
    conn.close()
    for entry in planned["users"]:
        written = audit[entry["user_id"]]
        assert (entry["type"], entry["after"]) == (written["type"], written["after"]), entry["user"]
        assert entry == written

    converted = run_report(db_file, "--json")
    assert converted.returncode == 0, converted.stdout + converted.stderr
    assert "already converted" in converted.stdout


@pytest.mark.slow
def test_report_refuses_a_database_before_022(tmp_path):
    db_file = legacy_database(tmp_path)
    conn = sqlite3.connect(db_file)
    conn.execute("update alembic_version set version_num = '020_post_archive'")
    conn.commit()
    conn.close()
    result = run_report(db_file)
    assert result.returncode == 2
    assert "022_builtin_roles" in result.stdout + result.stderr
