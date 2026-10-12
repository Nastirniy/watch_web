"""Dry run of migration 023_authorization_v2: the plan for every user of a database at revision 022_builtin_roles,
read-only.

    python -m app.backend.cli.authz_report [--json] [--url URL]

Run it on a copy of the production database before the upgrade. A database before 022 is refused: upgrade the copy
first (`alembic upgrade 022_builtin_roles` from the project root, WATCH_WEB_ALEMBIC_URL pointing at the copy). For
every user: the inferred user type, the roles, home post and access rows before, the assignments after (role @ node,
"root" or "p:<post id>"), the permissions dropped (dead without admin.true, or scoped permissions of a user without
any post) and the differences the new model cannot express; then the derived roles. Migration 023 writes the same
per-user record into its audit rows (action migrate, resource_type user): this is what it will do.

A database at 023 or later is converted already: the command says so and exits 0. Exit code 2: the database is not
at 022 (or is not an alembic database, or the sqlite file does not exist).

--url defaults to settings.database.url (an async URL like the application's, or a plain synchronous one).
"""
import argparse
import asyncio
import contextlib
import json
import sys
from pathlib import Path
from typing import Optional, Tuple

import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.backend.migrations.support.authz_v2_data import LegacyData, describe_plan, load_legacy

REVISION = "022_builtin_roles"
CONVERTED = "023_authorization_v2"
ALEMBIC_INI = Path(__file__).resolve().parents[3] / "alembic.ini"


def converted_revisions() -> set:
    """023 and every later revision"""
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))
    return {revision.revision for revision in script.walk_revisions(base=CONVERTED, head="heads")} | {CONVERTED}


def _read(conn) -> Tuple[Optional[str], Optional[LegacyData]]:
    """The revision of the database and, at 022, its legacy data. Read-only: the session refuses writes and the
    transaction is never committed."""
    if conn.dialect.name == "sqlite":
        conn.exec_driver_sql("PRAGMA query_only = ON")
    elif conn.dialect.name in ("mysql", "mariadb"):
        conn.exec_driver_sql("SET SESSION TRANSACTION READ ONLY")
    if "alembic_version" not in sa.inspect(conn).get_table_names():
        return None, None
    version = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
    return version, load_legacy(conn) if version == REVISION else None


async def _read_async(url: str):
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return await conn.run_sync(_read)
    finally:
        await engine.dispose()


def read_database(url: str) -> Tuple[Optional[str], Optional[LegacyData]]:
    if make_url(url).get_dialect().is_async:
        return asyncio.run(_read_async(url))
    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            return _read(conn)
    finally:
        engine.dispose()


def _missing_sqlite_file(url: str) -> bool:
    parsed = make_url(url)
    return parsed.get_backend_name() == "sqlite" and parsed.database not in (None, "", ":memory:") \
        and not Path(parsed.database).is_file()


def _list(values) -> str:
    return ", ".join(values) if values else "-"


def text_report(revision: str, report: dict) -> str:
    lines = [f"authz_report: database at {revision}, {len(report['users'])} user(s), "
             f"{len(report['derived_roles'])} derived role(s)", ""]
    for user in report["users"]:
        before = user["before"]
        roles = [f"{role['name']} ({role['id']}{', archived' if role['archived'] else ''})" for role in before["roles"]]
        rows = [f"{row['post_id']} ({'write' if row['can_write'] else 'read'})" for row in before["access"]]
        after = [f"{a['role']} @ {a['scope']}{' (derived)' if a['derived'] else ''}" for a in user["after"]]
        lines += [
            f"user {user['user_id']} {user['user']}: type {user['type']}",
            f"  before: roles {_list(roles)}; home post {before['post_id'] if before['post_id'] is not None else '-'};"
            f" access rows {_list(rows)}",
            f"  after: {_list(after)}",
            f"  dropped: {_list(user['dropped'])}",
            f"  differences: {_list(user['differences'])}",
            "",
        ]
    lines.append("derived roles:")
    for role in report["derived_roles"]:
        archived = " [archived]" if role["archived"] else ""
        lines.append(f"  {role['name']} \"{role['display_name']}\" from role {role['source_role_id']}{archived}: "
                     f"{_list(role['permissions'])}")
    if not report["derived_roles"]:
        lines.append("  -")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Dry run of migration 023_authorization_v2 on a database at 022")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--url", help="database URL (default: settings.database.url)")
    args = parser.parse_args(argv)
    if not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding="utf-8")   # piped or redirected: UTF-8 whatever the console code page

    url = args.url
    if url is None:
        with contextlib.redirect_stdout(sys.stderr):   # loading the settings prints a line: stdout is the report
            from app.backend.config import settings
        url = settings.database.url
    if _missing_sqlite_file(url):
        print(f"authz_report: no database file at {make_url(url).database}", file=sys.stderr)
        return 2

    revision, data = read_database(url)
    if revision in converted_revisions():
        message = f"already converted: the database is at {revision}"
        print(json.dumps({"revision": revision, "status": message}) if args.json else message)
        return 0
    if data is None:
        print(f"authz_report: the database is at {revision or 'no alembic revision'}; the report reads a database at "
              f"{REVISION} (upgrade a copy to {REVISION} first)", file=sys.stderr)
        return 2

    report = describe_plan(data, data.plan())
    if args.json:
        print(json.dumps({"revision": revision, **report}, ensure_ascii=False, indent=2))
    else:
        print(text_report(revision, report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
