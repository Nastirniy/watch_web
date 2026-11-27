"""The schema check of a role, without starting anything (RELEASE.md).

    python -m app.backend.cli.schema_check check --role api|migrate|worker
        prints the decision of bootstrap/schema_range.py in one line; exit 0 this release runs on the database,
        3 it refuses (the SCHEMA CHECK FAILED line), 2 the database is unreachable
    python -m app.backend.cli.schema_check status
        database_revision=, min_code_head=, code_head=, version= (one key=value line each; empty: none)

The api image runs `check --role api` before uvicorn; deploy/release.sh runs `check --role migrate` (deploy) or
`--role api` (rollback) from the candidate image before it changes anything, and `status` for its status report.
Read-only: it never creates, migrates or seeds the database.
"""
import argparse
import asyncio
import sys
from typing import Optional

from sqlalchemy.exc import OperationalError

from app.backend.bootstrap import schema_range
from app.backend.build_info import build_info
from app.backend.utils.alembic_history import code_head

UNKNOWN_DATABASE = 1049  # MariaDB / MySQL: the database does not exist yet; the api creates it at its start


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.backend.cli.schema_check", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    check = commands.add_parser("check", help="exit 0 when this release runs on the database, 3 when it refuses")
    check.add_argument("--role", required=True, choices=["api", "migrate", "worker"])
    commands.add_parser("status", help="database_revision, min_code_head, code_head, version")
    return parser


async def _state() -> tuple[Optional[str], Optional[str]]:
    try:
        return await schema_range.read_state()
    except OperationalError as exc:
        code = getattr(exc.orig, "args", (None,))[0]
        if code == UNKNOWN_DATABASE:
            return None, None
        raise


async def run(argv: list[str]) -> int:
    args = _parser().parse_args(argv)
    role = getattr(args, "role", "status")
    try:
        revision, min_code_head = await _state()
    except (OperationalError, OSError) as exc:
        print(f"SCHEMA CHECK FAILED ({role}): the database is unreachable ({type(exc).__name__}). Fix: check that "
              f"MariaDB runs and the MYSETTINGS_DATABASE__* values in .env, then try again.")
        return 2
    if args.command == "status":
        print(f"database_revision={revision or ''}")
        print(f"min_code_head={min_code_head or ''}")
        print(f"code_head={code_head()}")
        print(f"version={build_info().version}")
        return 0
    verdict = schema_range.decide(args.role, revision, min_code_head)
    print(verdict.message)
    return 0 if verdict.run else 3


async def _main(argv: list[str]) -> int:
    from app.backend.database import close_db
    try:
        return await run(argv)
    finally:
        await close_db()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main(sys.argv[1:])))
