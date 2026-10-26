"""Dry run of migration 023_authorization_v2: the plan for every user of a database at revision 022_builtin_roles,
read-only; with --v3 the dry run of migration 030_authorization_v3 on a database at 028 or 029.

    python -m app.backend.cli.authz_report [--json] [--url URL]
    python -m app.backend.cli.authz_report --v3 [--json] [--url URL]

--v3 prints, per user, the type after the conversion and the permissions in effect per node before and after it
(v2: every live role at its node; v3: kinds, split roles, system assignments at the root, the boundary of the new
type), every difference other than the unit management former post_admin holders gain (expected: none), the grants
the conversion drops and the system administrators after it. Exit code 2: the database is not at 028 or 029.

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
    "--alias <display name> --user-type system_administrator (DEPLOYMENT.md)"
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


V3_REVISIONS = ("028_no_actor_foreign_keys", "029_authz_v3_schema")


def _read_v3(conn):
    """(revision, v2 data for the v3 conversion or None, inactive user ids); read-only like _read"""
    from app.backend.migrations.support.authz_v3_data import load_v2
    if conn.dialect.name == "sqlite":
        conn.exec_driver_sql("PRAGMA query_only = ON")
    elif conn.dialect.name in ("mysql", "mariadb"):
        conn.exec_driver_sql("SET SESSION TRANSACTION READ ONLY")
    if "alembic_version" not in sa.inspect(conn).get_table_names():
        return None, None, frozenset()
    version = conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar()
    if version not in V3_REVISIONS:
        return version, None, frozenset()
    inactive = frozenset(user_id for user_id, is_active, is_deleted in conn.execute(
        sa.select(USERS.c.id, USERS.c.is_active, USERS.c.is_deleted)) if not is_active or is_deleted)
    return version, load_v2(conn), inactive


def v3_report(data, inactive) -> dict:
    """The v3 conversion of the data load_v2 read: per user, before and after"""
    from app.backend.migrations.support.authz_v3 import ALLOWED_GAIN, effective, plan_v3
    roles, assignments, users, taken = data
    plan = plan_v3(roles, assignments, users, taken)
    report_users, unexpected = [], []
    for user in users:
        before = effective(None, roles, assignments, users, user.id)
        after = effective(plan, roles, assignments, users, user.id)
        lost = {key: sorted(before[key] - after.get(key, frozenset())) for key in before
                if before[key] - after.get(key, frozenset())}
        gained = {key: sorted(after[key] - before.get(key, frozenset())) for key in after
                  if after[key] - before.get(key, frozenset()) - ALLOWED_GAIN}
        if lost or gained:
            unexpected.append({"user_id": user.id, "lost": lost, "gained": gained})
        report_users.append({
            "user_id": user.id, "type_before": user.user_type, "type_after": plan.user_types[user.id],
            "before": {key: sorted(names) for key, names in sorted(before.items())},
            "after": {key: sorted(names) for key, names in sorted(after.items())},
        })
    admin_holders = sorted({planned.user_id for planned in plan.system_assignments
                            if not isinstance(planned.role, str) and roles[planned.role].builtin_key == "admin"
                            and planned.user_id not in inactive})
    return {
        "users": report_users,
        "unexpected_differences": unexpected,
        "split_roles": [{"name": s.name, "source_role_id": s.source_id, "permissions": sorted(s.permissions),
                         "archived": s.is_deleted} for s in plan.split_roles],
        "renamed_roles": {str(role_id): name for role_id, name in plan.role_renames.items()},
        "dropped": [{"assignment_id": d.assignment_id, "reason": d.reason, "permissions": sorted(d.permissions)}
                    for d in plan.dropped],
        "system_administrators": admin_holders,
    }


def v3_text(revision: str, report: dict) -> str:
    lines = [f"authz_report --v3: database at {revision}, {len(report['users'])} user(s)",
             f"system administrators after the conversion (active): {len(report['system_administrators'])}"]
    if not report["system_administrators"]:
        lines.append(NO_ROOT_ADMIN)
    lines.append(f"unexpected differences: {len(report['unexpected_differences'])}")
    for item in report["unexpected_differences"]:
        lines.append(f"  user {item['user_id']}: lost {item['lost'] or '-'}, gained {item['gained'] or '-'}")
    lines.append("")
    for user in report["users"]:
        lines.append(f"user {user['user_id']}: type {user['type_before']} -> {user['type_after']}")
        for key, names in user["after"].items():
            lines.append(f"  {key}: {_list(names)}")
    lines.append("split roles: " + _list(f"{s['name']} from {s['source_role_id']}" for s in report["split_roles"]))
    lines.append("dropped grants: " + _list(f"{d['assignment_id']} ({d['reason']}: {', '.join(d['permissions'])})"
                                            for d in report["dropped"]))
    return "\n".join(lines)


def read_v3(url: str):
    if make_url(url).get_dialect().is_async:
        async def run():
            engine = create_async_engine(url)
            try:
                async with engine.connect() as conn:
                    return await conn.run_sync(_read_v3)
            finally:
                await engine.dispose()
        return asyncio.run(run())
    engine = sa.create_engine(url)
    try:
        with engine.connect() as conn:
            return _read_v3(conn)
    finally:
        engine.dispose()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Dry run of migration 023_authorization_v2 on a database at 022")
    parser.add_argument("--json", action="store_true", help="print the report as JSON")
    parser.add_argument("--url", help="database URL (default: settings.database.url)")
    parser.add_argument("--v3", action="store_true",
                        help="dry run of 030_authorization_v3 on a database at 028 or 029")
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

    if args.v3:
        revision, data, inactive = read_v3(url)
        if data is None:
            print(f"authz_report: the database is at {revision or 'no alembic revision'}; --v3 reads a database at "
                  f"{' or '.join(V3_REVISIONS)}", file=sys.stderr)
            return 2
        report = v3_report(data, inactive)
        print(json.dumps({"revision": revision, **report}, ensure_ascii=False, indent=2) if args.json
              else v3_text(revision, report))
        return 0

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
