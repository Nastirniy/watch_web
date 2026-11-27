"""Which code runs on which schema: the start check of every process (RELEASE.md, "Release rules for developers").

R = the database revision (alembic_version), H = this code's head, M = schema_compat.min_code_head (absent: BASELINE,
no contract since then; migrations/support/compat.py).

- R known to this code (or no revision yet): the api and `migrate` migrate up to H and seed; the media worker needs
  R = H, else it refuses ("start the api first, it migrates"). The other CLIs run in the api container, which passed
  the check at its start.
- R unknown (a newer release migrated): nobody migrates or seeds. Every role runs when M is known to this code and at
  or before H: the previous release runs on the new schema, which is the promise of every expand migration. The
  authorization catalog check then accepts built-in roles holding more permissions than this code knows.
- Otherwise the process refuses with ONE line (SchemaOutOfRange) and exits 3; the restart policy retries.

decide() is pure; check_schema() reads R and M and raises. The api image runs `cli/schema_check check --role api`
before uvicorn (the container exits 3 with the line) and the start checks again before and inside the schema lock.
"""
from dataclasses import dataclass
from typing import Literal, Optional

from app.backend.build_info import build_info
from app.backend.database import get_engine
from app.backend.migrations.support.compat import read_min_code_head, write_min_code_head  # noqa: F401
from app.backend.utils.alembic_history import History, history

import sqlalchemy as sa

# The head on main when the contract began (2026-11-26): revisions after it declare CONTRACT
BASELINE = "037_outbox_events"
EMPTY = "no revision (empty)"

Role = Literal["api", "migrate", "worker"]
MIGRATING_ROLES = ("api", "migrate")


@dataclass(frozen=True)
class SchemaVerdict:
    role: str
    revision: Optional[str]
    min_code_head: Optional[str]
    code_head: str
    known: bool
    migrate: bool
    seed: bool
    run: bool
    message: str


class SchemaOutOfRange(RuntimeError):
    """This code must not run on the database's schema; str() is the one line for the log"""

    def __init__(self, verdict: SchemaVerdict):
        super().__init__(verdict.message)
        self.verdict = verdict


def _refuse_behind(role: str, revision: Optional[str], head: str, version: str) -> str:
    return (f"SCHEMA CHECK FAILED ({role}): the database is at {revision or EMPTY}, release {version} needs {head}. "
            f"Fix: start the api first (docker compose up -d api); this worker retries by itself.")


def _refuse_newer(role: str, revision: str, head: str, admits: str, version: str) -> str:
    return (f"SCHEMA CHECK FAILED ({role}): the database is at {revision}, which release {version} (code head {head}) "
            f"does not know, and the database admits only code at {admits} or newer. Fix: deploy the newer release "
            f'again (deploy/release.sh deploy <tag>) or restore the pre-upgrade backup (RELEASE.md, "Restore the '
            f'database").')


def decide(role: Role, revision: Optional[str], min_code_head: Optional[str],
           hist: Optional[History] = None) -> SchemaVerdict:
    hist = hist or history()
    head = hist.head
    version = build_info().version

    def verdict(known: bool, run: bool, migrate: bool, message: str) -> SchemaVerdict:
        return SchemaVerdict(role=role, revision=revision, min_code_head=min_code_head, code_head=head, known=known,
                             migrate=migrate, seed=migrate, run=run, message=message)

    if revision is None or hist.knows(revision):
        if role in MIGRATING_ROLES:
            if revision == head:
                return verdict(True, True, True, f"Schema check: database at {head}, the head of release {version}")
            return verdict(True, True, True, f"Schema check: database at {revision or EMPTY}; release {version} "
                                             f"migrates it to {head}")
        if revision == head:
            return verdict(True, True, False, f"Schema check: database at {head}, the head of release {version}")
        return verdict(True, False, False, _refuse_behind(role, revision, head, version))

    admits = min_code_head or hist.baseline or BASELINE
    if hist.is_at_or_before(admits, head):
        return verdict(False, True, False,
                       f"Schema check: database at {revision}, newer than this release ({head}); allowed (min code "
                       f"head {min_code_head or 'none'}); not migrating, not seeding")
    return verdict(False, False, False, _refuse_newer(role, revision, head, admits, version))


def read_revision(conn: sa.Connection) -> Optional[str]:
    """alembic_version of the database; None without the table or its row"""
    if not sa.inspect(conn).has_table("alembic_version"):
        return None
    return conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()


async def read_state(engine=None) -> tuple[Optional[str], Optional[str]]:
    """(R, M) of the database"""
    engine = engine or await get_engine()
    async with engine.connect() as conn:
        return await conn.run_sync(lambda sync: (read_revision(sync), read_min_code_head(sync)))


async def check_schema(role: Role, engine=None) -> SchemaVerdict:
    """The verdict for this database; SchemaOutOfRange when this code must not run on it"""
    revision, min_code_head = await read_state(engine)
    result = decide(role, revision, min_code_head)
    if not result.run:
        raise SchemaOutOfRange(result)
    return result
