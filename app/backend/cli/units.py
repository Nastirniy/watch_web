"""Unit (corps, brigade) maintenance commands.

    python -m app.backend.cli.units fill-history [--dry-run]

fill-history converts old observations into the unit style: every observation that has no saved unit at all gets one
row for every unit its post is in now (services/unit_history.py). An observation that has any row (corps only,
brigade only, or both) is recorded history and is left as it is. Run it after the corps and brigades are set up and
the posts placed, and again after later placements; each run fills only observations still without a row. --dry-run
prints the same numbers and writes nothing. Safe while the API runs.
"""
import argparse
import asyncio
import sys
from typing import List

from app.backend.services.unit_history import HistoryFill, fill_unit_history


def report_lines(result: HistoryFill) -> List[str]:
    """The lines the command prints: the dry-run notice, one line per unit, the totals"""
    lines = ["dry run: nothing written"] if result.dry_run else []
    lines.extend(f"{unit.kind} {unit.group_id} {unit.name}: {unit.observations}" for unit in result.units)
    lines.append(f"observations: {result.observations}, rows: {result.rows}, posts in no unit: {result.posts_skipped}")
    return lines


async def fill_history(dry_run: bool) -> int:
    from app.backend.database import get_session_maker, close_db

    session_maker = await get_session_maker()
    try:
        async with session_maker() as session:
            result = await fill_unit_history(session, dry_run=dry_run)
        for line in report_lines(result):
            print(line)
    finally:
        await close_db()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Unit (corps, brigade) maintenance commands")
    sub = parser.add_subparsers(dest="command", required=True)

    fill = sub.add_parser("fill-history", help="give observations with no saved unit the corps and brigade their post is in now")
    fill.add_argument("--dry-run", action="store_true", help="print what would be filled, write nothing")

    args = parser.parse_args(argv)
    if args.command == "fill-history":
        return asyncio.run(fill_history(args.dry_run))
    return 1


if __name__ == "__main__":
    sys.exit(main())
