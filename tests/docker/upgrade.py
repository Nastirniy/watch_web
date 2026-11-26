"""Upgrade and rollback across releases on the compose.test.yml stack (RELEASE.md, "Release rules for developers").

    venv/Scripts/python tests/docker/upgrade.py

Builds three images from the checked-out commit, the last two from temporary copies that are never committed:
  A  the commit itself
  B  A plus an expand migration (a nullable throwaway column, CONTRACT = False)
  C  B plus a contract migration that names itself (drops the column, set_min_code_head to its own revision)
and walks the promise of option b on a fresh database:
  1. A starts and migrates; ready.
  2. B migrates with the migrate command while A serves; B starts; ready at B's head.
  3. rollback to A: A starts on B's schema without migrating or seeding; ready, login and a list answer.
  4. C migrates and starts.
  5. the schema check of A refuses C's schema (exit 3, the one line), as release.sh rollback does.
  6. A started by hand on C's schema exits 3 with SCHEMA CHECK FAILED (api) and never becomes healthy.
Exit 0 when every step passes. The stack is removed at the end (down -v), also on a failure.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from app.backend.api.v1 import V1  # noqa: E402  (paths from the route enums, CLAUDE.md)
from app.backend.enums.routes import AuthRoutes, PostRoutes, SystemRoutes  # noqa: E402
from app.backend.utils.alembic_history import code_head  # noqa: E402
COMPOSE = ["docker", "compose", "-f", str(ROOT / "tests" / "docker" / "compose.test.yml")]
NGINX = f"http://127.0.0.1:{os.environ.get('E2E_NGINX_PORT', '18089')}"
LOGIN = {
    "certificate_cn": "admin@radiomonitoring.local",
    "certificate_serial": "ABC123DEF456",
    "certificate_fingerprint": "12:34:56:78:90:AB:CD:EF:12:34:56:78:90:AB:CD:EF:12:34:56:78",
}
TAGS = {"A": "upgrade-a", "B": "upgrade-b", "C": "upgrade-c"}

EXPAND = '''"""Upgrade test only (tests/docker/upgrade.py, never committed): a nullable throwaway column

Revision ID: {rev}
Revises: {down}
"""
from alembic import op
import sqlalchemy as sa

revision = "{rev}"
down_revision = "{down}"
CONTRACT = False


def upgrade():
    op.add_column("posts", sa.Column("upgrade_probe", sa.String(16), nullable=True))


def downgrade():
    op.drop_column("posts", "upgrade_probe")
'''

CONTRACT = '''"""Upgrade test only (tests/docker/upgrade.py, never committed): a contract that admits only itself

Revision ID: {rev}
Revises: {down}
"""
from alembic import op

from app.backend.migrations.support.compat import set_min_code_head

revision = "{rev}"
down_revision = "{down}"
CONTRACT = True


def upgrade():
    op.drop_column("posts", "upgrade_probe")
    set_min_code_head(op, "{rev}")


def downgrade():
    raise NotImplementedError("upgrade test only")
'''

failures: list[str] = []


def check(ok: bool, name: str, detail: str = "") -> None:
    print(f"[{'ok' if ok else 'FAIL':4}] {name}" + (f": {detail}" if detail and not ok else ""))
    if not ok:
        failures.append(name)


def run(args: list[str], env: dict | None = None, check_exit: bool = True, timeout: int = 1800) -> subprocess.CompletedProcess:
    result = subprocess.run(args, cwd=ROOT, env={**os.environ, **(env or {})}, capture_output=True, text=True,
                            timeout=timeout)
    if check_exit and result.returncode != 0:
        print(result.stdout[-3000:], result.stderr[-3000:], sep="\n")
        raise SystemExit(f"command failed: {' '.join(args)}")
    return result


def build(tag: str, context: Path) -> None:
    if os.environ.get("UPGRADE_REUSE_IMAGES") and run(["docker", "image", "inspect", f"watch-web:{tag}"],
                                                       check_exit=False).returncode == 0:
        print(f"       reusing watch-web:{tag}")
        return
    started = time.time()
    run(["docker", "build", "-q", "--build-arg", f"WATCH_WEB_VERSION={tag}", "-t", f"watch-web:{tag}", str(context)])
    print(f"       built watch-web:{tag} in {time.time() - started:.0f} s")


def copy_of_commit(target: Path) -> None:
    archive = target.parent / "src.tar"
    run(["git", "archive", "-o", str(archive), "HEAD"])
    with tarfile.open(archive) as tar:
        tar.extractall(target, filter="data")
    archive.unlink()


def compose(tag: str, *args: str, check_exit: bool = True, timeout: int = 1800) -> subprocess.CompletedProcess:
    return run([*COMPOSE, *args], env={"WATCH_WEB_TAG": tag}, check_exit=check_exit, timeout=timeout)


def wait_ready(expected_version: str, seconds: int = 600) -> dict | None:
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            response = requests.get(NGINX + SystemRoutes.HEALTH_READY.value, timeout=5)
            if response.status_code == 200 and response.json().get("version") == expected_version:
                return response.json()
        except (requests.ConnectionError, requests.Timeout, ValueError):
            pass
        time.sleep(3)
    return None


def api_logs(tag: str) -> str:
    return compose(tag, "logs", "--no-color", "api", check_exit=False).stdout


def smoke() -> bool:
    try:
        token = requests.post(NGINX + V1.path(AuthRoutes.LOGIN), json=LOGIN, timeout=10)
        if token.status_code != 200:
            return False
        headers = {"Authorization": f"Bearer {token.json()['access_token']}"}
        return requests.get(NGINX + V1.path(PostRoutes.ROOT), headers=headers, timeout=10).status_code == 200
    except (requests.ConnectionError, requests.Timeout, ValueError, KeyError):
        return False


def up_until_ready(tag: str, seconds: int = 900) -> dict | None:
    """up -d again while a dependency is still starting (the first MySQL start, the first migration), then wait"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        compose(tag, "up", "-d", check_exit=False)
        ready = wait_ready(tag, seconds=60)
        if ready is not None:
            return ready
    print(compose(tag, "ps", "-a", "--format", "{{.Service}} {{.State}} {{.Health}}", check_exit=False).stdout)
    print(api_logs(tag)[-3000:])
    return None


def migrate(tag: str) -> subprocess.CompletedProcess:
    return compose(tag, "run", "--rm", "-T", "--no-deps", "--entrypoint", "python", "api", "-m",
                   "app.backend.cli.migrations_manager", "migrate", check_exit=False)


def schema_check(tag: str, role: str) -> subprocess.CompletedProcess:
    return compose(tag, "run", "--rm", "-T", "--no-deps", "--entrypoint", "python", "api", "-m",
                   "app.backend.cli.schema_check", "check", "--role", role, check_exit=False)


def main() -> int:
    base = code_head()
    rev_b, rev_c = "998_upgrade_expand", "999_upgrade_contract"
    work = Path(tempfile.mkdtemp(prefix="ww-upgrade-"))
    try:
        print("Building A, B, C")
        build(TAGS["A"], ROOT)
        versions = work / "b" / "app" / "backend" / "migrations" / "versions"
        copy_of_commit(work / "b")
        (versions / f"{rev_b}.py").write_text(EXPAND.format(rev=rev_b, down=base), encoding="utf-8")
        build(TAGS["B"], work / "b")
        shutil.copytree(work / "b", work / "c")
        versions_c = work / "c" / "app" / "backend" / "migrations" / "versions"
        (versions_c / f"{rev_c}.py").write_text(CONTRACT.format(rev=rev_c, down=rev_b), encoding="utf-8")
        build(TAGS["C"], work / "c")

        compose(TAGS["A"], "down", "-v", check_exit=False)
        print("1. A on a fresh database")
        compose(TAGS["A"], "up", "-d", "mysql", "init", check_exit=False)
        ready = up_until_ready(TAGS["A"])
        check(ready is not None and ready["schema_revision"] == base, "A ready at its head", str(ready))

        print("2. B migrates while A serves, then B starts")
        started = time.time()
        result = migrate(TAGS["B"])
        check(result.returncode == 0, f"B migrates ({time.time() - started:.0f} s)", result.stdout[-500:])
        check(smoke(), "A still serves on B's schema before the switch")
        ready = up_until_ready(TAGS["B"])
        check(ready is not None and ready["schema_revision"] == rev_b, "B ready at its head", str(ready))

        print("3. rollback to A on B's schema")
        check(schema_check(TAGS["A"], "api").returncode == 0, "A's schema check admits B's schema")
        ready = up_until_ready(TAGS["A"])
        check(ready is not None and ready["schema_revision"] == rev_b, "A ready on B's schema", str(ready))
        logs = api_logs(TAGS["A"])
        check(f"Schema check: database at {rev_b}, newer than this release ({base})" in logs,
              "A logs: newer schema, not migrating, not seeding")
        check(smoke(), "A serves: login and a list")

        print("4. C (contract) migrates and starts")
        result = migrate(TAGS["C"])
        check(result.returncode == 0, "C migrates", result.stdout[-500:])
        ready = up_until_ready(TAGS["C"])
        check(ready is not None and ready["schema_revision"] == rev_c, "C ready at its head", str(ready))

        print("5. a rollback to A is refused")
        result = schema_check(TAGS["A"], "api")
        line = [text for text in result.stdout.splitlines() if text.startswith("SCHEMA CHECK FAILED")]
        check(result.returncode == 3 and bool(line), "A's schema check refuses C's schema with exit 3",
              result.stdout[-500:])
        check(bool(line) and f"admits only code at {rev_c} or newer" in line[0], "the line names the contract",
              str(line))

        print("6. A started by hand on C's schema")
        compose(TAGS["A"], "up", "-d", "--no-deps", "api", check_exit=False)
        time.sleep(30)
        state = compose(TAGS["A"], "ps", "-a", "--format", "{{.Service}} {{.State}} {{.Health}}", "api",
                        check_exit=False).stdout
        logs = api_logs(TAGS["A"])
        check("healthy" not in state.replace("unhealthy", ""), "A never becomes healthy", state)
        check("SCHEMA CHECK FAILED (api): the database is at 999_upgrade_contract" in logs,
              "A logs SCHEMA CHECK FAILED (api)")
    finally:
        compose(TAGS["A"], "down", "-v", check_exit=False)
        shutil.rmtree(work, ignore_errors=True)
    print()
    print("all steps passed" if not failures else f"{len(failures)} step(s) failed: {failures}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
