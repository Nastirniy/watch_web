#!/usr/bin/env python3
"""
Database backups (run in the db-backup tool service: docker compose run --rm db-backup <command>; BACKUPS.md).

Usage:
    python -m app.backend.cli.db_backup create [--label manual|pre-upgrade|scheduled] [--no-copy]
        # dump the database into backup.dir (and copy_dir), then apply retention; prints the id
    python -m app.backend.cli.db_backup list [--json]           # backups, newest first; incomplete folders
    python -m app.backend.cli.db_backup verify <id|latest> [--deep]
        # checksum and gzip; --deep also loads it into <db>_verify and compares tables and revision
    python -m app.backend.cli.db_backup restore <id|latest> [--yes] [--force-connections]
        # roll the database back to a backup; the replaced tables stay in <db>_before_<time>
    python -m app.backend.cli.db_backup undo-restore [<schema>] [--yes] [--force-connections]
        # swap the newest (or the named) <db>_before_* schema back in
    python -m app.backend.cli.db_backup prune [--dry-run]       # retention, and kept schemas past keep_previous_days
    python -m app.backend.cli.db_backup status [--json]         # what GET /database/backups reports

Exit codes: 0 ok, 1 failed, 2 not configured or bad usage, 3 refused (nothing was changed).
Stop api and media-worker before restore and undo-restore (they refuse while other connections use the database).
"""
import argparse
import dataclasses
import json
import os
import sys
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

from sqlalchemy.exc import OperationalError

from app.backend.config import settings
from app.backend.enums.general import BackupLabel
from app.backend.services.backups.catalog import load_catalog
from app.backend.services.backups.client_tools import find_tools
from app.backend.services.backups.dump import create_backup, prune
from app.backend.services.backups.failures import BackupFailure
from app.backend.services.backups.locking import writer_lock
from app.backend.services.backups.restore import (
    MariaDbBackend, RestoreRefused, RestoreResult, plan_restore, prune_kept_schemas, restore_backup, undo_restore,
    verify_backup,
)

EXIT_OK, EXIT_FAILED, EXIT_USAGE, EXIT_REFUSED = 0, 1, 2, 3


def _backend() -> MariaDbBackend:
    return MariaDbBackend(settings.database, find_tools(), settings.backup.dump_timeout_seconds)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _plain(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return list(value)
    raise TypeError(type(value).__name__)


def _json(data) -> None:
    print(json.dumps(data, default=_plain, indent=2))


def _size(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB"):
        if n < 1024 or unit == "GiB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def _age(seconds: Optional[int]) -> str:
    if seconds is None:
        return "never"
    hours, minutes = divmod(seconds // 60, 60)
    return f"{hours}h{minutes:02d}m ago"


def _kept(backend) -> Optional[list]:
    try:
        return [{"schema": k.name, "created_at": k.created_at, "size_bytes": k.size_bytes}
                for k in backend.kept_schemas(settings.database.name)]
    except OperationalError as exc:
        print(f"previous states: database not reachable ({type(exc.orig).__name__})", file=sys.stderr)
        return None


def cmd_create(args) -> int:
    backend = _backend()
    result = create_backup(settings.backup, settings.database, BackupLabel(args.label), copy=not args.no_copy,
                           object_guard=lambda: backend.unsupported_objects(settings.database.name),
                           tools=find_tools())
    print(result.manifest.id)
    return EXIT_OK


def cmd_list(args) -> int:
    now = _now()
    catalog = load_catalog(settings.backup, now)
    if args.json:
        _json({"backups": [{**dataclasses.asdict(e.manifest), "copied": e.copied} for e in catalog.entries],
               "incomplete": [dataclasses.asdict(i) for i in catalog.incomplete]})
        return EXIT_OK
    for entry in catalog.entries:
        m = entry.manifest
        copied = {None: "-", True: "copied", False: "NOT copied"}[entry.copied]
        print(f"{m.id}  {_size(m.size_bytes):>10}  {_age(int((now - m.finished_at).total_seconds())):>12}  "
              f"{m.migration_revision}  {copied}")
    for item in catalog.incomplete:
        print(f"incomplete: {item.name} ({item.reason})")
    if not catalog.entries:
        print("no backups")
    return EXIT_OK


def cmd_status(args) -> int:
    catalog = load_catalog(settings.backup, _now())
    kept = _kept(_backend())
    summary = catalog.summary
    if args.json:
        _json({"summary": dataclasses.asdict(summary), "previous_states": kept or []})
        return EXIT_OK
    print(f"last success: {summary.last_success_id or 'never'} ({_age(summary.age_seconds)})"
          f"{', STALE' if summary.stale else ''} (stale after {summary.stale_after_hours}h)")
    if summary.last_run is not None:
        run = summary.last_run
        print(f"last run: {run.outcome}{' ' + run.error_code if run.error_code else ''} at {_plain(run.finished_at)}")
    print(f"backups: {summary.count}, {_size(summary.total_size_bytes)}; free {_size(summary.free_bytes)}")
    if summary.copy.configured:
        print(f"copy: {'reachable' if summary.copy.reachable else 'NOT reachable'}, {summary.copy.missing} missing")
    for state in kept or []:
        print(f"previous state: {state['schema']} ({_size(state['size_bytes'])})")
    return EXIT_OK


def cmd_verify(args) -> int:
    result = verify_backup(settings.backup, settings.database, args.ref, deep=args.deep,
                           backend=_backend() if args.deep else None)
    words = {True: "ok", False: "mismatch", None: "not checked"}
    print(f"{result.backup_id}: checksum: {words[result.checksum_ok]}, gzip: {words[result.gzip_ok]}, "
          f"tables: {words[result.tables_ok]}, revision: {words[result.revision_ok]}")
    checks = [result.checksum_ok, result.gzip_ok, result.tables_ok, result.revision_ok]
    return EXIT_OK if all(c is not False for c in checks) else EXIT_FAILED


def _confirm(prompt: str) -> bool:
    print(prompt, end="", flush=True)
    return sys.stdin.readline().strip() == "yes"


def _report(result: RestoreResult) -> None:
    carry = result.carry
    print(f"the replaced tables are kept in {result.kept_schema} (undo-restore swaps them back)")
    print(f"carried over: {carry.revocations} revocation(s), {carry.certificates_added} certificate(s) added as "
          f"revoked, CRL number {carry.crl_number}, {carry.sessions_ended} session(s) ended, "
          f"{result.ids_raised} id counter(s) raised past the replaced data")
    for user_id, username in carry.users_added:
        print(f"user {user_id} {username}: added archived (created after the backup; its certificates revoked)")
    for user_id, username, change in result.user_differences:
        print(f"user {user_id} {username}: {change} (left as restored)")
    print("next: docker compose up -d")
    # the tool service has no PKI settings of its own: name the step for servers with PKI either way
    print("next, with PKI: docker compose exec api python -m app.backend.cli.pki crl (publish the CRL now)")
    print("note: media files uploaded after the backup stay on disk without rows (BACKUPS.md)")


def cmd_restore(args) -> int:
    backend = _backend()
    if not args.yes:
        plan = plan_restore(settings.backup, settings.database, args.ref, backend=backend)
        print(f"backup {plan.backup_id} ({plan.label}, {_plain(plan.started_at)}), revision {plan.backup_revision}")
        print(f"live database {settings.database.name}, revision {plan.live_revision}, "
              f"{plan.other_connections} other connection(s)")
        if not _confirm("Type yes to restore: "):
            print("aborted", file=sys.stderr)
            return EXIT_REFUSED
    _report(restore_backup(settings.backup, settings.database, args.ref, backend=backend,
                           force_connections=args.force_connections))
    return EXIT_OK


def cmd_undo(args) -> int:
    backend = _backend()
    if not args.yes and not _confirm(f"Swap {args.schema or 'the newest kept schema'} back into "
                                     f"{settings.database.name}? Type yes: "):
        print("aborted", file=sys.stderr)
        return EXIT_REFUSED
    _report(undo_restore(settings.backup, settings.database, args.schema, backend=backend,
                         force_connections=args.force_connections))
    return EXIT_OK


def cmd_prune(args) -> int:
    now = _now()
    verb = "would delete" if args.dry_run else "deleted"
    # one writer at a time: a restore or a create must not lose the schema or folder it is working on
    with writer_lock(Path(settings.backup.dir)):
        for backup in prune(settings.backup, now, dry_run=args.dry_run):
            print(f"{verb} backup {backup}")
        for schema in prune_kept_schemas(settings.backup, settings.database, backend=_backend(), now=now,
                                         dry_run=args.dry_run):
            print(f"{verb} kept schema {schema}")
    return EXIT_OK


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="db_backup", description="Database backups (BACKUPS.md)")
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create")
    create.add_argument("--label", default=BackupLabel.MANUAL.value, choices=[label.value for label in BackupLabel])
    create.add_argument("--no-copy", action="store_true")
    create.set_defaults(handler=cmd_create)
    for name, handler in (("list", cmd_list), ("status", cmd_status)):
        command = commands.add_parser(name)
        command.add_argument("--json", action="store_true")
        command.set_defaults(handler=handler)
    verify = commands.add_parser("verify")
    verify.add_argument("ref")
    verify.add_argument("--deep", action="store_true")
    verify.set_defaults(handler=cmd_verify)
    restore = commands.add_parser("restore")
    restore.add_argument("ref")
    undo = commands.add_parser("undo-restore")
    undo.add_argument("schema", nargs="?")
    for command, handler in ((restore, cmd_restore), (undo, cmd_undo)):
        command.add_argument("--yes", action="store_true")
        command.add_argument("--force-connections", action="store_true")
        command.set_defaults(handler=handler)
    prune_command = commands.add_parser("prune")
    prune_command.add_argument("--dry-run", action="store_true")
    prune_command.set_defaults(handler=cmd_prune)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    os.umask(0o027)   # folders 0750, manifests 0640 (the api reads them through the backup group), dumps 0600
    args = _parser().parse_args(argv)
    if not settings.backup.is_configured():
        print("backups are not configured (MYSETTINGS_BACKUP__DIR)", file=sys.stderr)
        return EXIT_USAGE
    try:
        return args.handler(args)
    except BackupFailure as exc:
        print(f"{'refused' if exc.refused else 'failed'}: {exc.code}", file=sys.stderr)
        return EXIT_REFUSED if exc.refused else EXIT_FAILED
    except RestoreRefused as exc:
        print(f"refused: {exc.code}", file=sys.stderr)
        return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
