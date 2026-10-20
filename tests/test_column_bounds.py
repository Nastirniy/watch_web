"""MySQL column sizes and INT bounds. sqlite enforces neither, so these tests check the guards themselves:
the column type, the cut of request-derived strings, the request bounds and the migration."""
import importlib.util
import os
import sqlite3
import subprocess
import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from pydantic import ValidationError
from sqlmodel import select
from starlette.requests import Request

from app.backend.api.shared.helpers.audit_helper import client_info
from app.backend.api.v1 import V1
from app.backend.api.v1.models.request.observations import (
    ObservationCreate, ObservationFrequencyCreate, ObservationFrequencyUpdate, ObservationUpdate,
)
from app.backend.enums.routes import ObservationRoutes
from app.backend.models.audit import AuditLog
from app.backend.models.media import MediaFile
from app.backend.models.observation import Observation
from tests.helpers import make_dictionary, make_observation, make_post, make_user
from tests.test_migrations import HEAD, PROJECT_ROOT, run_migrate

INT_MAX = 2147483647
DISAPPEARED = V1.path(ObservationRoutes.MARK_DISAPPEARED)


def test_file_size_is_a_big_integer():
    """Uploads allow 10 GB; a MySQL INT stops at 2 GiB"""
    assert isinstance(MediaFile.__table__.c.file_size.type, sa.BigInteger)


def _request(url_path: str, user_agent: str) -> Request:
    return Request({
        "type": "http", "method": "GET", "scheme": "http", "server": ("testserver", 80), "path": "/search",
        "raw_path": b"/search", "query_string": url_path.encode(), "root_path": "",
        "headers": [(b"user-agent", user_agent.encode())], "client": ("127.0.0.1", 1234),
    })


def test_client_info_cuts_request_derived_strings_to_the_column_lengths():
    columns = AuditLog.__table__.c
    info = client_info(_request("q=" + "x" * 2000, "agent/" + "y" * 2000))
    assert len(info["request_url"]) == columns.request_url.type.length == 500
    assert info["request_url"].startswith("http://testserver/search?q=xxx")
    assert len(info["user_agent"]) == 255
    short = client_info(_request("q=1", "curl/8"))
    assert short["request_url"] == "http://testserver/search?q=1" and short["user_agent"] == "curl/8"


def test_audit_user_agent_column_has_an_explicit_length():
    """AutoString is VARCHAR(255) on MySQL; the model says so, the cut in client_info reads it"""
    assert AuditLog.__table__.c.user_agent.type.length == 255


FREQUENCY = {"kind": "rc", "frequency_min": "100.000", "frequency_max": "200.000"}


@pytest.mark.parametrize("build", [
    lambda v: ObservationFrequencyCreate(**FREQUENCY, link_group=v),
    lambda v: ObservationFrequencyUpdate(link_group=v),
    lambda v: ObservationCreate(post_id=1, signal_type_id=1, frequencies=[FREQUENCY], duration_seconds=v),
    lambda v: ObservationUpdate(duration_seconds=v),
])
def test_integer_fields_stop_at_the_int_range(build):
    assert build(INT_MAX) is not None
    with pytest.raises(ValidationError) as caught:
        build(INT_MAX + 1)
    assert caught.value.errors()[0]["type"] == "less_than_equal"


@pytest.mark.asyncio
async def test_disappeared_duration_that_does_not_fit_int_is_refused(client, db, headers):
    post = await make_post(db)
    ids = await make_dictionary(db)
    user = await make_user(db, perms=["observation.view", "observation.update"], post_id=post.id)
    observation = await make_observation(db, user, post, ids["signal_type_id"])
    url = DISAPPEARED.format(observation_id=observation.id)

    # the bounds of a request body keep a duration this long out of reach; only a row stored before them can have it
    row = (await db.exec(select(Observation).where(Observation.id == observation.id))).one()
    row.observed_at = datetime(1900, 1, 1)
    db.add(row)
    await db.commit()
    far = datetime.now(timezone.utc)
    refused = await client.post(url, json={"disappeared_at": far.isoformat()}, headers=headers(user))
    assert refused.status_code == 422, refused.text
    body = refused.json()
    assert body["code"] == "validation_error"
    assert body["errors"][0]["code"] == "value_too_large"
    assert body["errors"][0]["pointer"] == "#/disappeared_at"

    row.observed_at = datetime.now(timezone.utc) - timedelta(hours=1)
    db.add(row)
    await db.commit()
    now = datetime.now(timezone.utc)
    accepted = await client.post(url, json={"disappeared_at": now.isoformat()}, headers=headers(user))
    assert accepted.status_code == 200, accepted.text


def _load_018():
    path = PROJECT_ROOT / "app" / "backend" / "migrations" / "versions" / "018_media_file_size_bigint.py"
    spec = importlib.util.spec_from_file_location("migration_018", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_018_alters_only_a_plain_integer_column_on_mysql():
    module = _load_018()
    assert module.needs_bigint(sa.Integer(), "mysql") is True
    assert module.needs_bigint(sa.BigInteger(), "mysql") is False
    assert module.needs_bigint(sa.Integer(), "sqlite") is False, "sqlite INTEGER is already 64-bit"
    assert module.needs_integer(sa.BigInteger(), "mysql") is True
    assert module.needs_integer(sa.Integer(), "mysql") is False
    assert module.needs_integer(sa.BigInteger(), "sqlite") is False


@contextmanager
def _sizes_table(tmp_path, *sizes):
    """Connection to a sqlite media_files table holding these file sizes"""
    engine = sa.create_engine(f"sqlite:///{(tmp_path / 'sizes.db').as_posix()}")
    try:
        with engine.begin() as conn:
            conn.execute(sa.text("create table media_files (id integer primary key, file_size bigint not null)"))
            for size in sizes:
                conn.execute(sa.text("insert into media_files (file_size) values (:size)"), {"size": size})
        with engine.connect() as conn:
            yield conn
    finally:
        engine.dispose()


@pytest.mark.parametrize("sizes", [(), (1, INT_MAX)])
def test_018_downgrade_guard_passes_sizes_that_fit_an_int(tmp_path, sizes):
    module = _load_018()
    with _sizes_table(tmp_path, *sizes) as conn:
        module.check_sizes_fit_integer(conn)


def test_018_downgrade_guard_refuses_a_size_over_an_int(tmp_path):
    """A non-strict MySQL sql_mode clamps an out-of-range value to 2147483647 instead of failing the ALTER"""
    module = _load_018()
    with _sizes_table(tmp_path, 10, INT_MAX + 1) as conn:
        with pytest.raises(RuntimeError) as refused:
            module.check_sizes_fit_integer(conn)
    message = str(refused.value)
    assert str(INT_MAX + 1) in message and str(INT_MAX) in message and "media_files.file_size" in message


def test_018_downgrade_checks_the_sizes_before_the_alter(tmp_path, monkeypatch):
    module = _load_018()
    altered = []
    with _sizes_table(tmp_path, 5000000000) as conn:
        fake_op = type("FakeOp", (), {
            "get_bind": staticmethod(lambda: conn),
            "alter_column": staticmethod(lambda *args, **kwargs: altered.append((args, kwargs))),
        })
        monkeypatch.setattr(module, "op", fake_op)
        monkeypatch.setattr(module, "needs_integer", lambda column_type, dialect_name: True)
        with pytest.raises(RuntimeError):
            module.downgrade()
    assert altered == [], "the column is not touched while a size does not fit"


def _run_downgrade(db_file, revision):
    env = dict(os.environ)
    env["WATCH_WEB_ALEMBIC_URL"] = f"sqlite+aiosqlite:///{db_file.as_posix()}"
    env["PYTHONPATH"] = str(PROJECT_ROOT)
    return subprocess.run(
        [sys.executable, "-m", "app.backend.cli.migrations_manager", "downgrade", revision],
        cwd=PROJECT_ROOT, env=env, capture_output=True, text=True, timeout=300,
    )


def test_018_up_and_down_on_sqlite_keep_the_rows(tmp_path):
    db_file = tmp_path / "bigint.db"
    first = run_migrate(db_file)
    assert first.returncode == 0, first.stdout + first.stderr

    conn = sqlite3.connect(db_file)
    conn.execute(
        "insert into media_files (original_filename, stored_filename, file_path, file_size, mime_type, file_extension,"
        " media_type, file_hash, uploaded_at, is_deleted, poster_attempts) values ('a.mp4', 's.mp4', 'p/s.mp4', 5000000000, 'video/mp4',"
        " 'mp4', 'video', 'h', '2026-09-01 00:00:00', 0, 0)")
    conn.execute("update alembic_version set version_num = '017_video_storyboards'")
    conn.commit()
    conn.close()

    second = run_migrate(db_file)
    assert second.returncode == 0, second.stdout + second.stderr
    conn = sqlite3.connect(db_file)
    assert conn.execute("select version_num from alembic_version").fetchone()[0] == HEAD
    assert conn.execute("select file_size from media_files").fetchone() == (5000000000,)
    conn.close()

    # down: a no-op on sqlite (INTEGER is 64-bit), the row survives
    down = _run_downgrade(db_file, "017_video_storyboards")
    assert down.returncode == 0, down.stdout + down.stderr
    conn = sqlite3.connect(db_file)
    assert conn.execute("select version_num from alembic_version").fetchone()[0] == "017_video_storyboards"
    assert conn.execute("select file_size from media_files").fetchone() == (5000000000,)
    conn.close()
