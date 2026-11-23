#!/usr/bin/env python3
"""
Reconcile the media directory and the PKI store with the database after a restore (run in the api container;
BACKUPS.md, "Restore (rollback)").

Usage:
    python -m app.backend.cli.restore_reconcile            # dry run: what it would do
    python -m app.backend.cli.restore_reconcile --apply    # do it (one audit row)

The work is services/backups/reconcile.py. Media files no row names go to <upload_path>/_orphaned/<time>/, user guide
files and folders no user_guide_files row names (and .tmp- folders of interrupted guide uploads) to
<upload_path>/_orphaned/<time>/_guides/, certificate packages no row names (private keys) to
<store_dir>/.quarantine/<time>/. A file a row names again that sits under _orphaned (an undo after an --apply) moves
back; rows whose file is nowhere are listed. Nothing younger than 10 minutes is moved, hidden, NAS and Windows
metadata names are left alone, and nothing is deleted but derived posters and storyboards of ids without a row.
Exit code 1: something could not be moved (logged).
"""
import argparse
import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.config import settings
from app.backend.enums.audit import AuditEvent
from app.backend.services.audit import create_audit_log
from app.backend.services.backups.reconcile import ReconcileReport, apply_reconcile, plan_reconcile

SECTIONS = (
    ("orphan_files", "media file without a row", "moved to _orphaned"),
    ("stale_derived", "derived file of an id without a row", "deleted"),
    ("returned_originals", "original of a row found under _orphaned", "moved back"),
    ("orphan_packages", "certificate package without a row", "moved to .quarantine"),
    ("orphan_guide_files", "user guide file or folder without a row", "moved to _orphaned"),
)


def _print(report: ReconcileReport, applied: bool) -> None:
    for field, what, action in SECTIONS:
        items = getattr(report, field)
        print(f"{len(items)} {what}{' (' + action + ')' if applied else ''}")
        for item in items:
            print(f"  {item}")
    print(f"{len(report.missing_originals)} row(s) whose original is missing: {list(report.missing_originals)}")
    print(f"{len(report.missing_guide_files)} user guide file row(s) whose file is missing")
    for item in report.missing_guide_files:
        print(f"  guide {item.guide_id} file {item.file_id}: {item.file_name}")
    for item in report.failed:
        print(f"FAILED: {item} (see the log)")


async def run(session: AsyncSession, *, apply: bool, upload_path: Path, store_dir: Optional[Path],
              now: datetime) -> int:
    report = await plan_reconcile(session, upload_path, store_dir, now)
    if not apply:
        _print(report, applied=False)
        print("dry run: nothing changed; run again with --apply")
        return 0
    done = await asyncio.to_thread(apply_reconcile, report, upload_path, store_dir, now)
    _print(done, applied=True)
    # The event's params are part of the API contract: user guide files count as files (orphan_files) and as
    # originals (missing_originals); the printed report and the log tell them apart
    await create_audit_log(session, AuditEvent.DATABASE_RECONCILE, params={
        "orphan_files": len(done.orphan_files) + len(done.orphan_guide_files), "stale_derived": len(done.stale_derived),
        "returned_originals": len(done.returned_originals),
        "missing_originals": len(done.missing_originals) + len(done.missing_guide_files),
        "orphan_packages": len(done.orphan_packages), "failed": len(done.failed)})
    await session.commit()
    return 1 if done.failed else 0


async def _main(apply: bool) -> int:
    from app.backend.database import close_db, get_session_maker
    session_maker = await get_session_maker()
    try:
        async with session_maker() as session:
            store = Path(settings.pki.store_dir) if settings.pki.store_dir else None
            return await run(session, apply=apply, upload_path=Path(settings.storage.upload_path), store_dir=store,
                             now=datetime.now(timezone.utc))
    finally:
        await close_db()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="restore_reconcile", description=__doc__.split("\n\n")[0])
    parser.add_argument("--apply", action="store_true", help="move and delete (default: dry run)")
    args = parser.parse_args(argv)
    return asyncio.run(_main(args.apply))


if __name__ == "__main__":
    sys.exit(main())
