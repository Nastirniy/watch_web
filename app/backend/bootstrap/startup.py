"""Startup of the API: schema check, migrate and seed once, serialised across the uvicorn workers."""
import asyncio
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import text

from app.backend.bootstrap.schema_range import Role, SchemaOutOfRange, SchemaVerdict, check_schema
from app.backend.bootstrap.seed import seed_database
from app.backend.database import create_database_if_not_exists, get_engine, get_session_maker
from app.backend.utils.alembic_history import ALEMBIC_INI
from app.backend.utils.logging_config import get_database_logger

_logger = get_database_logger()


SCHEMA_LOCK_NAME = "watch_web_schema_init"
# How long one GET_LOCK call waits before the worker logs that it is still waiting and asks again
SCHEMA_LOCK_POLL_SECONDS = 30


class _SchemaLock:
    """MySQL advisory lock held while one process migrates and seeds.

    Every uvicorn worker runs the lifespan, so without this two workers run
    Alembic at the same time and MySQL rejects one with a concurrent DDL
    error. GET_LOCK is connection-scoped: the connection stays open until
    release, and the server frees the lock when the holder's connection
    closes (its process stopped or crashed). So a worker waits as long as
    another connection holds the lock, however long the migration takes
    (a large backfill runs for minutes), logging every poll interval; no
    deadline fails the waiting workers while the holder still works.
    Other dialects (sqlite in tests) have no such lock and skip it.
    """

    def __init__(self):
        self._conn = None

    async def __aenter__(self):
        engine = await get_engine()
        if engine.dialect.name != "mysql":
            return self
        self._conn = await engine.connect()
        try:
            await self._wait_for_lock()
        except BaseException:
            await self._conn.close()
            self._conn = None
            raise
        _logger.info("Schema lock acquired; running migrations and seeding")
        return self

    async def _wait_for_lock(self):
        waited = 0
        while True:
            acquired = (await self._conn.execute(
                text("SELECT GET_LOCK(:name, :timeout)"),
                {"name": SCHEMA_LOCK_NAME, "timeout": SCHEMA_LOCK_POLL_SECONDS},
            )).scalar()
            if acquired == 1:
                return
            if acquired is None:  # an error in the server (out of memory, the thread killed), not a timeout
                raise RuntimeError(f"GET_LOCK of schema lock {SCHEMA_LOCK_NAME} failed")
            waited += SCHEMA_LOCK_POLL_SECONDS
            holder = (await self._conn.execute(
                text("SELECT IS_USED_LOCK(:name)"), {"name": SCHEMA_LOCK_NAME},
            )).scalar()
            _logger.info(f"Schema lock {SCHEMA_LOCK_NAME} is held by connection {holder} (migrating); "
                         f"waited {waited}s, still waiting")

    async def __aexit__(self, *exc):
        if self._conn is not None:
            try:
                await self._conn.execute(text("SELECT RELEASE_LOCK(:name)"), {"name": SCHEMA_LOCK_NAME})
            finally:
                await self._conn.close()
                self._conn = None
            _logger.info("Schema lock released")


def run_alembic_upgrade() -> None:
    """alembic upgrade head of this code (blocking; run in a thread)"""
    from alembic import command
    from alembic.config import Config
    command.upgrade(Config(str(ALEMBIC_INI)), "head")


async def initialize_database_with_migrations():
    """The start of the api: schema check, then migrate and seed once, serialised across processes"""
    await run_locked_start("api")


async def run_locked_start(role: Role) -> SchemaVerdict:
    """Schema check (bootstrap/schema_range.py) before the lock, again inside it (another process may have migrated
    while this one waited), then what the verdict allows: upgrade, seeding, the start checks. Shared by the api's
    lifespan and `migrations_manager migrate`, so the two never run alembic at once. SchemaOutOfRange: the one line
    is logged and raised."""
    await create_database_if_not_exists()
    engine = await get_engine()
    await _checked(role, engine)
    async with _SchemaLock():
        verdict = await _checked(role, engine)
        _logger.info(verdict.message)
        try:
            if verdict.migrate:
                # A migration that fails stops the startup: no create_all fallback, which would create the missing
                # tables only and leave the schema half migrated (CLAUDE.md, "Database Migrations")
                loop = asyncio.get_running_loop()
                with ThreadPoolExecutor() as executor:
                    await loop.run_in_executor(executor, run_alembic_upgrade)
                _logger.info("Migrations applied successfully")
                await seed_database()
            await _check_unit_structure()
            await _check_authorization(newer_schema=not verdict.known)
            _logger.info("Database initialization completed successfully")
        except Exception as e:
            _logger.error(f"Failed to initialize database: {e}")
            raise
    return verdict


async def _checked(role: Role, engine) -> SchemaVerdict:
    try:
        return await check_schema(role, engine)
    except SchemaOutOfRange as refused:
        _logger.error(str(refused))
        raise


async def _check_authorization(newer_schema: bool = False):
    """Permission levels or built-in roles out of step with the code stop the start; grants that leave permissions
    out and a missing system administrator are logged (bootstrap/authorization.py). On a schema newer than this
    code (no seeding), built-in roles may hold permissions a newer release added."""
    from app.backend.bootstrap.authorization import check_authorization_catalog, log_authorization_report
    async with (await get_session_maker())() as session:
        await check_authorization_catalog(session, allow_extra=newer_schema)
        await log_authorization_report(session)


async def _check_unit_structure():
    """A corps or brigade in the wrong place stops the start (bootstrap/structure.py)"""
    from app.backend.bootstrap.structure import check_unit_structure
    async with (await get_session_maker())() as session:
        await check_unit_structure(session)
