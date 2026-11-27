"""Schema compatibility (RELEASE.md, "Release rules for developers"): which code runs on which schema.

The previous release's code runs on the new schema; a contract migration names the oldest code head that still runs
(schema_compat.min_code_head), and older code refuses with one line. decide() is pure; the start, the worker and the
CLI read the revision and min_code_head from the database and act on its verdict.
"""
import asyncio
import json
import os

import pytest
import sqlalchemy as sa
from sqlmodel import select

from app.backend import build_info as build_info_module
from app.backend.bootstrap import schema_range, startup
from app.backend.bootstrap.schema_range import BASELINE, SchemaOutOfRange, decide
from app.backend.utils.alembic_history import History, code_head, history, known_revisions

FAKE = History(parents={"a": (), "b": ("a",), "c": ("b",)}, head="c")
FUTURE = "999_future"


class _Lines:
    def __init__(self):
        self.lines = []

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@pytest.fixture
def caplog_loguru():
    from loguru import logger
    captured = _Lines()
    sink = logger.add(lambda message: captured.lines.append(message.record["message"]), level="INFO")
    yield captured
    logger.remove(sink)


@pytest.fixture(autouse=True)
def release_version(tmp_path, monkeypatch):
    path = tmp_path / "build_info.json"
    path.write_text(json.dumps({"version": "2026.11.20-73a93fa", "revision": None, "created": None}))
    monkeypatch.setattr(build_info_module, "BUILD_INFO_PATH", path)
    build_info_module.build_info.cache_clear()
    yield
    build_info_module.build_info.cache_clear()


# ---------------------------------------------------------------- the alembic history

def test_the_real_history_knows_the_baseline_and_the_head():
    real = history()
    assert real.head == code_head()
    assert real.knows(BASELINE) and BASELINE in known_revisions()
    assert real.is_at_or_before(BASELINE, real.head)
    assert real.is_at_or_before("001_relativize_media_paths", BASELINE)
    assert not real.is_at_or_before(real.head, "001_relativize_media_paths")


def test_fake_history_order():
    assert FAKE.is_at_or_before("a", "c") and FAKE.is_at_or_before("c", "c")
    assert not FAKE.is_at_or_before("c", "b")
    assert not FAKE.knows("d") and not FAKE.is_at_or_before("d", "c")


# ---------------------------------------------------------------- decide (pure)

RUN_AND_MIGRATE = ("api", "migrate")
RUN_ONLY = ("worker",)


@pytest.mark.parametrize("role", RUN_AND_MIGRATE)
@pytest.mark.parametrize("revision", [None, "a", "b", "c"])
@pytest.mark.parametrize("min_code_head", [None, "a", "b"])
def test_the_api_and_migrate_migrate_a_known_schema(role, revision, min_code_head):
    verdict = decide(role, revision, min_code_head, FAKE)
    assert verdict.known and verdict.run and verdict.migrate and verdict.seed
    assert verdict.code_head == "c"


@pytest.mark.parametrize("role", RUN_ONLY)
@pytest.mark.parametrize("min_code_head", [None, "a"])
def test_worker_and_cli_run_only_at_the_code_head(role, min_code_head):
    verdict = decide(role, "c", min_code_head, FAKE)
    assert verdict.run and not verdict.migrate and not verdict.seed


@pytest.mark.parametrize("role", RUN_ONLY)
@pytest.mark.parametrize("revision", [None, "a", "b"])
def test_worker_and_cli_refuse_a_schema_behind_their_code(role, revision):
    verdict = decide(role, revision, None, FAKE)
    assert verdict.known and not verdict.run and not verdict.migrate


@pytest.mark.parametrize("role", RUN_AND_MIGRATE + RUN_ONLY)
@pytest.mark.parametrize("min_code_head", ["a", "b", "c"])
def test_every_role_runs_without_migrating_on_a_newer_schema_that_admits_it(role, min_code_head):
    verdict = decide(role, "d", min_code_head, FAKE)
    assert not verdict.known
    assert verdict.run and not verdict.migrate and not verdict.seed


@pytest.mark.parametrize("role", RUN_AND_MIGRATE + RUN_ONLY)
@pytest.mark.parametrize("min_code_head", ["d", "e"])
def test_every_role_refuses_a_newer_schema_that_admits_only_newer_code(role, min_code_head):
    verdict = decide(role, "e", min_code_head, FAKE)
    assert not verdict.run and not verdict.migrate and not verdict.seed


def test_absent_min_code_head_means_the_baseline():
    real = history()
    assert decide("api", FUTURE, None, real).run, "no contract since the baseline: this code runs"
    old = History(parents={"x": ()}, head="x")
    assert not decide("api", FUTURE, None, old).run, "code that does not know the baseline refuses"


def test_the_refusal_line_of_the_api():
    verdict = decide("api", "e", "e", FAKE)
    assert verdict.message == (
        'SCHEMA CHECK FAILED (api): the database is at e, which release 2026.11.20-73a93fa (code head c) does not '
        'know, and the database admits only code at e or newer. Fix: deploy the newer release again '
        '(deploy/release.sh deploy <tag>) or restore the pre-upgrade backup (RELEASE.md, "Restore the database").'
    )


def test_the_refusal_line_of_the_worker():
    verdict = decide("worker", "b", None, FAKE)
    assert verdict.message == (
        "SCHEMA CHECK FAILED (worker): the database is at b, release 2026.11.20-73a93fa needs c. Fix: start the api "
        "first (docker compose up -d api); this worker retries by itself."
    )
    empty = decide("worker", None, None, FAKE)
    assert empty.message.startswith("SCHEMA CHECK FAILED (worker): the database is at no revision (empty), ")


def test_cli_is_not_a_role():
    """Review M6: no command checks as a role of its own; the CLIs run in the api container, which passed the check"""
    from app.backend.cli import schema_check
    assert set(schema_range.Role.__args__) == {"api", "migrate", "worker"}
    with pytest.raises(SystemExit):
        asyncio.run(schema_check.run(["check", "--role", "cli"]))


def test_the_line_of_a_start_on_a_newer_schema():
    assert decide("api", "d", "b", FAKE).message == (
        "Schema check: database at d, newer than this release (c); allowed (min code head b); "
        "not migrating, not seeding"
    )
    assert "(min code head none)" in decide("api", "d", None, History(parents={**FAKE.parents}, head="c",
                                                                        baseline="a")).message


# ---------------------------------------------------------------- the start of the api on a database

@pytest.fixture
def start_on(engine, session_maker, monkeypatch):
    """The start of bootstrap/startup.py on the tests' database; upgrade and seeding are recorded, not run"""
    calls = []

    async def get_engine():
        return engine

    async def get_session_maker():
        return session_maker

    async def nothing():
        return None

    async def seed():
        calls.append("seed")

    monkeypatch.setattr(startup, "get_engine", get_engine)
    monkeypatch.setattr(startup, "get_session_maker", get_session_maker)
    monkeypatch.setattr(startup, "create_database_if_not_exists", nothing)
    monkeypatch.setattr(startup, "seed_database", seed)
    monkeypatch.setattr(startup, "run_alembic_upgrade", lambda: calls.append("upgrade"))
    return calls


async def set_revision(engine, revision: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(sa.text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32) NOT NULL)"))
        await conn.execute(sa.text("DELETE FROM alembic_version"))
        await conn.execute(sa.text("INSERT INTO alembic_version VALUES (:v)"), {"v": revision})


async def set_min(engine, revision: str) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync: schema_range.write_min_code_head(sync, revision))


async def give_extra_permission_to_administrator(db) -> None:
    from app.backend.enums.general import BuiltinRole
    from app.backend.models.role import Permission, Role, RolePermission
    extra = Permission(name="future.view", display_name="future", level="tree")
    db.add(extra)
    await db.flush()
    role = (await db.exec(select(Role).where(Role.builtin_key == BuiltinRole.ADMINISTRATOR.value))).one()
    db.add(RolePermission(role_id=role.id, permission_id=extra.id))
    await db.commit()


async def test_previous_release_starts_on_a_newer_schema(db, engine, start_on, client, caplog_loguru):
    await set_revision(engine, FUTURE)
    await give_extra_permission_to_administrator(db)
    await startup.initialize_database_with_migrations()
    assert start_on == [], "no upgrade, no seeding on a schema newer than the code"
    assert "Schema check: database at 999_future, newer than this release" in caplog_loguru.text
    assert "future.view" in caplog_loguru.text, "the extra stored permission is named once"
    from app.backend.enums.routes import SystemRoutes
    assert (await client.get(SystemRoutes.HEALTH.value)).status_code in (200, 429)


async def test_the_catalog_check_stays_strict_on_a_known_schema(db, engine, start_on):
    await set_revision(engine, code_head())
    await give_extra_permission_to_administrator(db)
    with pytest.raises(RuntimeError, match="Authorization catalog out of step"):
        await startup.initialize_database_with_migrations()
    assert start_on == ["upgrade", "seed"]


async def test_code_refuses_a_contract_schema(db, engine, start_on):
    await set_revision(engine, FUTURE)
    await set_min(engine, FUTURE)
    with pytest.raises(SchemaOutOfRange) as refused:
        await startup.initialize_database_with_migrations()
    assert str(refused.value).startswith("SCHEMA CHECK FAILED (api): the database is at 999_future, which release ")
    assert start_on == []


async def test_two_lifespans_decide_the_same(db, engine, start_on):
    await set_revision(engine, FUTURE)
    await asyncio.gather(startup.initialize_database_with_migrations(), startup.initialize_database_with_migrations())
    assert start_on == []


async def test_a_known_schema_is_migrated_and_seeded(db, engine, start_on):
    await set_revision(engine, BASELINE)
    await startup.initialize_database_with_migrations()
    assert start_on == ["upgrade", "seed"]


async def test_the_check_runs_again_inside_the_lock(db, engine, start_on, monkeypatch):
    """Another process migrated while this one waited for the lock: the verdict inside the lock wins"""
    await set_revision(engine, BASELINE)
    original = startup._SchemaLock.__aenter__

    async def enter_after_a_newer_release_migrated(self):
        await set_revision(engine, FUTURE)
        await set_min(engine, FUTURE)
        return await original(self)

    monkeypatch.setattr(startup._SchemaLock, "__aenter__", enter_after_a_newer_release_migrated)
    with pytest.raises(SchemaOutOfRange):
        await startup.initialize_database_with_migrations()
    assert start_on == []


# ---------------------------------------------------------------- worker, migrate CLI, schema_check CLI

async def test_worker_refuses_a_schema_behind_its_code(db, engine, monkeypatch):
    from app.backend.workers import media_worker

    async def get_engine():
        return engine

    monkeypatch.setattr(schema_range, "get_engine", get_engine)
    await set_revision(engine, BASELINE if BASELINE != code_head() else "001_relativize_media_paths")
    assert await media_worker.check_schema_or_exit() == 3


async def test_worker_runs_at_the_code_head(db, engine, monkeypatch):
    from app.backend.workers import media_worker

    async def get_engine():
        return engine

    monkeypatch.setattr(schema_range, "get_engine", get_engine)
    await set_revision(engine, code_head())
    assert await media_worker.check_schema_or_exit() is None


async def test_migrate_cli_takes_the_schema_lock(db, engine, start_on, monkeypatch):
    from app.backend.cli import migrations_manager
    events = []
    original_enter, original_exit = startup._SchemaLock.__aenter__, startup._SchemaLock.__aexit__

    async def enter(self):
        events.append("lock")
        return await original_enter(self)

    async def leave(self, *exc):
        events.append("unlock")
        return await original_exit(self, *exc)

    monkeypatch.setattr(startup._SchemaLock, "__aenter__", enter)
    monkeypatch.setattr(startup._SchemaLock, "__aexit__", leave)
    monkeypatch.setattr(startup, "run_alembic_upgrade", lambda: events.append("upgrade"))
    await set_revision(engine, BASELINE)
    await migrations_manager.migrate_locked()
    assert events == ["lock", "upgrade", "unlock"]
    assert start_on == ["seed"]


async def test_schema_check_cli_status_and_exit_codes(db, engine, monkeypatch, capsys):
    from app.backend.cli import schema_check

    async def get_engine():
        return engine

    monkeypatch.setattr(schema_range, "get_engine", get_engine)
    await set_revision(engine, FUTURE)
    assert await schema_check.run(["status"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == [f"database_revision={FUTURE}", "min_code_head=", f"code_head={code_head()}",
                   "version=2026.11.20-73a93fa"]

    assert await schema_check.run(["check", "--role", "api"]) == 0
    assert capsys.readouterr().out.startswith("Schema check: database at 999_future, newer than this release")

    await set_min(engine, FUTURE)
    assert await schema_check.run(["check", "--role", "migrate"]) == 3
    assert capsys.readouterr().out.startswith("SCHEMA CHECK FAILED (migrate): ")
    assert await schema_check.run(["status"]) == 0
    assert f"min_code_head={FUTURE}" in capsys.readouterr().out.splitlines()

    await set_revision(engine, code_head())
    await set_min(engine, BASELINE)
    assert await schema_check.run(["check", "--role", "worker"]) == 0


async def test_schema_check_cli_on_an_empty_database(engine, monkeypatch, capsys):
    from app.backend.cli import schema_check

    async def get_engine():
        return engine

    monkeypatch.setattr(schema_range, "get_engine", get_engine)
    assert await schema_check.run(["check", "--role", "api"]) == 0, "the api migrates an empty database"
    assert await schema_check.run(["check", "--role", "worker"]) == 3
    assert "no revision (empty)" in capsys.readouterr().out


# ---------------------------------------------------------------- set_min_code_head in a migration

def test_set_min_code_head_creates_the_table_once(tmp_path):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from app.backend.migrations.support.compat import set_min_code_head

    engine = sa.create_engine(f"sqlite:///{(tmp_path / 'compat.db').as_posix()}")
    with engine.begin() as conn:
        op = Operations(MigrationContext.configure(conn))
        set_min_code_head(op, "038_a")
        set_min_code_head(op, "039_b")
        assert conn.execute(sa.text("SELECT id, min_code_head FROM schema_compat")).all() == [(1, "039_b")]
        assert schema_range.read_min_code_head(conn) == "039_b"
        set_min_code_head(op, None)
        assert schema_range.read_min_code_head(conn) is None
    with engine.connect() as conn:
        assert schema_range.read_min_code_head(conn) is None
    engine.dispose()


def test_absent_table_reads_as_no_contract(tmp_path):
    engine = sa.create_engine(f"sqlite:///{(tmp_path / 'none.db').as_posix()}")
    with engine.connect() as conn:
        assert schema_range.read_min_code_head(conn) is None
        assert schema_range.read_revision(conn) is None
    engine.dispose()


def test_schema_compat_is_left_out_of_autogenerate():
    from app.backend.migrations.support.autogenerate import UNMODELLED_TABLES
    assert "schema_compat" in UNMODELLED_TABLES


def test_the_api_image_checks_the_schema_before_uvicorn():
    """With --workers uvicorn restarts a worker whose lifespan failed, for ever: the container would never exit.
    The check before uvicorn exits the container with 3 and the one line; Docker's restart policy backs off."""
    import re
    from tests.paths import PROJECT_ROOT
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    cmd = json.loads(re.search(r"^CMD (\[.*\])$", dockerfile, re.M).group(1))
    assert "python -m app.backend.cli.schema_check check --role api & " in cmd[2]
    assert "trap " in cmd[2] and "wait " in cmd[2], "a SIGTERM during the check stops it (review M2)"
    assert cmd[2].index("schema_check") < cmd[2].index("exec python -m uvicorn ")


@pytest.mark.skipif(os.name == "nt", reason="POSIX signals")
def test_a_stop_during_the_schema_check_is_immediate():
    """Review M2: sh is PID 1 while the check runs; a SIGTERM must stop the container at once, not after the grace"""
    import re
    import signal
    import subprocess
    import time
    from tests.paths import PROJECT_ROOT
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    script = json.loads(re.search(r"^CMD (\[.*\])$", dockerfile, re.M).group(1))[2]
    script = script.replace("python -m app.backend.cli.schema_check check --role api", "sleep 30")
    script = re.sub(r"exec python -m uvicorn .*$", "exec sleep 30", script)
    process = subprocess.Popen(["sh", "-c", script])
    time.sleep(1)
    started = time.monotonic()
    process.send_signal(signal.SIGTERM)
    code = process.wait(timeout=10)
    assert time.monotonic() - started < 3 and code == 143
