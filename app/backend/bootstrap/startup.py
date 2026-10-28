"""Startup of the API: migrate and seed once, serialised across the uvicorn workers."""
from sqlalchemy import text

from app.backend.bootstrap.seed import seed_database
from app.backend.database import create_database_if_not_exists, get_engine
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


async def initialize_database_with_migrations():
    """Initialize database using migrations and then seed data, serialised across processes"""
    await create_database_if_not_exists()
    async with _SchemaLock():
        await _migrate_and_seed()


async def _migrate_and_seed():
    import asyncio
    from concurrent.futures import ThreadPoolExecutor
    from alembic.config import Config
    from alembic import command

    _logger.info("Initializing database with migrations...")

    try:

        # A migration that fails stops the startup: no create_all fallback, which would create the missing
        # tables only and leave the schema half migrated (CLAUDE.md, "Database Migrations")
        def run_migrations():
            from pathlib import Path
            project_root = Path(__file__).resolve().parents[3]  # where alembic.ini is
            command.upgrade(Config(str(project_root / "alembic.ini")), "head")

        # Run migrations in a thread to avoid event loop issues
        loop = asyncio.get_running_loop()
        with ThreadPoolExecutor() as executor:
            await loop.run_in_executor(executor, run_migrations)
        _logger.info("Migrations applied successfully")
        
        # Always seed initial data
        await seed_database()
        await _check_unit_structure()
        await _check_authorization()
        _logger.info("Database initialization completed successfully")
        
    except Exception as e:
        _logger.error(f"Failed to initialize database: {e}")
        raise


async def _check_authorization():
    """Permission levels or built-in roles out of step with the code stop the start; grants that leave permissions
    out and a missing system administrator are logged (bootstrap/authorization.py)"""
    from app.backend.bootstrap.authorization import check_authorization_catalog, log_authorization_report
    from app.backend.database import get_session_maker
    async with (await get_session_maker())() as session:
        await check_authorization_catalog(session)
        await log_authorization_report(session)


async def _check_unit_structure():
    """A corps or brigade in the wrong place stops the start (bootstrap/structure.py)"""
    from app.backend.bootstrap.structure import check_unit_structure
    from app.backend.database import get_session_maker
    async with (await get_session_maker())() as session:
        await check_unit_structure(session)
