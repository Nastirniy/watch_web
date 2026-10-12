"""Dry run of migration 023_authorization_v2: the plan for every user of a database at revision 022_builtin_roles,
read-only.

    python -m app.backend.cli.authz_report [--json] [--url URL]

Run it on a copy of the production database before the upgrade. A database before 022 is refused: upgrade the copy
first (`alembic upgrade 022_builtin_roles` from the project root, WATCH_WEB_ALEMBIC_URL pointing at the copy). For
every user: the inferred user type, the roles, home post and access rows before, the assignments after (role @ node,
"root" or "p:<post id>"), the permissions dropped (dead without admin.true, or scoped permissions of a user without
any post) and the differences the new model cannot express; then the derived roles. Migration 023 writes the same
per-user record into its audit rows (action migrate, resource_type user): this is what it will do.

The first lines name the administrators after the conversion: the users who will hold the built-in admin role at the
root and are active and not archived (root_admins in --json), the users the last-administrator rule counts. With none
(the legacy administrator role was not the admin role, for example) a WARNING line says so: after the upgrade, archive
and deactivation answer last_admin_required and nobody can assign the admin role; create an administrator on the
server with python -m app.backend.cli.init_user <certificate.pem> --username <name> --alias <display name>
--user-type admin.

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
from typing import FrozenSet, List, Optional, Tuple

import sqlalchemy as sa
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from app.backend.auth.scope import ROOT
from app.backend.migrations.support.authz_v2 import ConversionPlan
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


USERS = sa.table("users", sa.column("id", sa.Integer), sa.column("is_active", sa.Boolean),
                 sa.column("is_deleted", sa.Boolean))

Database = Tuple[Optional[str], Optional[LegacyData], FrozenSet[int]]


def _read(conn) -> Database:
    """The revision of the database and, at 022, its legacy data and the users who cannot act (deactivated or
    archived). Read-only: the session refuses writes and the transaction is never committed."""
    if conn.dialect.name == "sqlite":
        conn.exec_driver_sql("PRAGMA query_only = ON")
    elif conn.dialect.name in ("mysql", "mariadb"):
        conn.exec_driver_sql("SET SESSION TRANSACTION READ ONLY")
    if "alembic_version" not in sa.inspect(conn).get_table_names():
        return None, None, frozenset()
    version = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
    if version != REVISION:
        return version, None, frozenset()
    inactive = frozenset(user_id for user_id, is_active, is_deleted in conn.execute(
        sa.select(USERS.c.id, USERS.c.is_active, USERS.c.is_deleted)) if not is_active or is_deleted)
    return version, load_legacy(conn), inactive


async def _read_async(url: str):
    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            return await conn.run_sync(_read)
    finally:
        await engine.dispose()


def read_database(url: str) -> Database:
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


def root_admins(data: LegacyData, plan: ConversionPlan, inactive: FrozenSet[int]) -> List[dict]:
    """The users who will hold the built-in admin role at the root and are active and not archived (the users
    privileges.check_an_admin_remains counts), by id"""
    names = {user.id: user.username for user in data.users}
    if data.admin_role_id is None:
        return []
    return [{"user_id": user_plan.user_id, "user": names[user_plan.user_id]} for user_plan in plan.users
            if user_plan.user_id not in inactive
            and any(a.role == data.admin_role_id and a.node == ROOT.key for a in user_plan.assignments)]


NO_ROOT_ADMIN = (
    "WARNING: nobody active will hold the built-in admin role at the root after the conversion: archiving or "
    "deactivating a user will answer last_admin_required and nobody can assign the admin role. After the upgrade, "
    "create an administrator on the server: python -m app.backend.cli.init_user <certificate.pem> --username <name> "
    "--alias <display name> --user-type admin (DEPLOYMENT.md)"
)


def text_report(revision: str, report: dict) -> str:
    admins = report["root_admins"]
    lines = [f"authz_report: database at {revision}, {len(report['users'])} user(s), "
             f"{len(report['derived_roles'])} derived role(s)",
             f"administrators after the conversion (the built-in admin role at the root, active, not archived): "
             f"{len(admins)}" + (f" ({', '.join(admin['user'] for admin in admins)})" if admins else "")]
    if not admins:
        lines.append(NO_ROOT_ADMIN)
    lines.append("")
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

    revision, data, inactive = read_database(url)
    if revision in converted_revisions():
        message = f"already converted: the database is at {revision}"
        print(json.dumps({"revision": revision, "status": message}) if args.json else message)
        return 0
    if data is None:
        print(f"authz_report: the database is at {revision or 'no alembic revision'}; the report reads a database at "
              f"{REVISION} (upgrade a copy to {REVISION} first)", file=sys.stderr)
        return 2

    plan = data.plan()
    report = {**describe_plan(data, plan), "root_admins": root_admins(data, plan, inactive)}
    if args.json:
        print(json.dumps({"revision": revision, **report}, ensure_ascii=False, indent=2))
    else:
        print(text_report(revision, report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
