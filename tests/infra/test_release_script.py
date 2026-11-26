"""deploy/release.sh, run in a temporary project directory with stub `docker` and `git` on PATH (RELEASE.md).

The stubs record every call and answer from rules: a rule is a shell glob matched against the arguments, an exit
code and an output. No real docker is touched. Runs wherever a POSIX sh exists (Git for Windows' sh included).
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.paths import PROJECT_ROOT

SCRIPT = PROJECT_ROOT / "deploy" / "release.sh"
SH = shutil.which("sh")
pytestmark = pytest.mark.skipif(SH is None, reason="needs a POSIX sh")

TAG_OLD = "2026.11.20-73a93fa"
TAG_NEW = "2026.11.24-2a8c1e7"
IMGPROXY = "ghcr.io/imgproxy/imgproxy:v3.31.4"
IMGPROXY_ID = "sha256:" + "ab" * 32

STUB = r'''#!/bin/sh
args="$*"
printf '%s\n' "$args" >> "$STUB_DIR/calls.log"
printf '%s|%s\n' "${WATCH_WEB_TAG:-}" "$args" >> "$STUB_DIR/env.log"
for rule in "$STUB_DIR"/rules/*; do
  [ -f "$rule" ] || continue
  pattern=$(sed -n 1p "$rule")
  case "$args" in
    $pattern)
      code=$(sed -n 2p "$rule")
      sed -n '3,$p' "$rule"
      exit "$code";;
  esac
done
exit 0
'''


class Project:
    def __init__(self, root: Path):
        self.root = root
        self.stub = root / "stub"
        self.bin = root / "bin"
        self.backups = root / "backups"
        for directory in (self.stub / "rules", self.bin, self.root / "deploy", self.backups):
            directory.mkdir(parents=True, exist_ok=True)
        for name in ("docker", "git"):
            path = self.bin / name
            path.write_text(STUB, newline="\n")
            path.chmod(0o755)
        self.count = 0
        self.env_text = (
            "# watch-web\n"
            "MYSETTINGS_DATABASE__PASSWORD=secret\n"
            f"WATCH_WEB_TAG={TAG_OLD}\n"
            f"IMGPROXY_IMAGE={IMGPROXY}\n"
            f"IMGPROXY_IMAGE_ID={IMGPROXY_ID}\n"
            f"BACKUP_DIR={self.backups.as_posix()}\n"
            "MEDIA_ROOT=/srv/media\n"
        )
        (root / ".env").write_text(self.env_text, newline="\n")
        (root / "docker-compose.yml").write_text("services: {}\n", newline="\n")  # docker itself is a stub
        self.healthy_setup()

    # ------------------------------------------------------------ rules
    def rule(self, pattern: str, code: int = 0, out: str = "") -> None:
        """First matching rule wins; rules added later are checked FIRST (a test overrides the defaults)"""
        self.count += 1
        name = f"{1000 - self.count:04d}"
        (self.stub / "rules" / name).write_text(f"{pattern}\n{code}\n{out}", newline="\n")

    def healthy_setup(self, tag: str = TAG_NEW) -> None:
        self.rule("version --format*", 0, "27.3.1\n")
        self.rule("compose version --short", 0, "2.29.7\n")
        self.rule("compose --profile tools config --services", 0, "api\nmedia-worker\nimgproxy\nmigrate\ndb-backup\n")
        self.rule("image inspect --format {{.Id}} " + IMGPROXY, 0, IMGPROXY_ID + "\n")
        self.rule("image inspect watch-web:* --format*", 0, "2a8c1e7aaaa 2026-11-24T09:12:00Z\n")
        self.rule("compose run --rm -T --no-deps --entrypoint python migrate -m app.backend.cli.schema_check check*", 0,
                  "Schema check: database at 037_outbox_events; release x migrates it to 038_x\n")
        # a rollback target (role api) runs on the newer schema without migrating
        self.rule("compose run --rm -T --no-deps --entrypoint python migrate -m app.backend.cli.schema_check check "
                  "--role api", 0, "Schema check: database at 038_x, newer than this release (037_outbox_events); "
                  "allowed (min code head none); not migrating, not seeding\n")
        self.rule("compose run --rm -T --no-deps --entrypoint python migrate -m app.backend.cli.schema_check status", 0,
                  "INFO: Loaded configuration for environment: production\ndatabase_revision=037_outbox_events\n"
                  "min_code_head=\ncode_head=037_outbox_events\nversion=" + TAG_OLD + "\n")
        self.rule("compose run --rm -T db-backup create --label pre-upgrade", 0, "20261124T101420Z-pre-upgrade\n")
        self.ps(tag)

    def ps(self, tag: str, api_health: str = "healthy") -> None:
        self.rule("compose ps *--format*", 0,
                  f"api|watch-web:{tag}|running|{api_health}\nmedia-worker|watch-web:{tag}|running|\n"
                  f"imgproxy|{IMGPROXY}|running|healthy\n")

    # ------------------------------------------------------------ run and look
    def run(self, *args: str, answer: str = "", extra_env=None) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env.update({"PATH": str(self.bin) + os.pathsep + env.get("PATH", ""), "STUB_DIR": self.stub.as_posix(),
                    "RELEASE_POLL_SECONDS": "0", "RELEASE_WAIT_SECONDS": "2", "RELEASE_MIN_FREE_MB": "1",
                    "USER": "operator"})
        env.pop("WATCH_WEB_TAG", None)
        env.update(extra_env or {})
        return subprocess.run([SH, SCRIPT.as_posix(), *args], cwd=self.root, env=env, input=answer,
                              capture_output=True, text=True, timeout=60)

    def calls(self) -> list[str]:
        path = self.stub / "calls.log"
        return path.read_text().splitlines() if path.exists() else []

    def calls_with_tag(self) -> list[str]:
        path = self.stub / "env.log"
        return path.read_text().splitlines() if path.exists() else []

    def changing_calls(self) -> list[str]:
        changing = ("compose up", "db-backup create", "compose run --rm -T migrate", "image rm", "build ", "load")
        return [call for call in self.calls() if any(word in call for word in changing)]

    @property
    def env(self) -> str:
        return (self.root / ".env").read_text()

    @property
    def history(self) -> str:
        path = self.root / "deploy" / "release-history.log"
        return path.read_text() if path.exists() else ""

    def add_history(self, *lines: str) -> None:
        with open(self.root / "deploy" / "release-history.log", "a", newline="\n") as f:
            for line in lines:
                f.write(line + "\n")


@pytest.fixture
def project(tmp_path) -> Project:
    return Project(tmp_path)


def fix_line(result) -> str:
    lines = [line for line in result.stderr.splitlines() if line.startswith("release.sh: ")]
    assert lines, result.stdout + result.stderr
    return lines[-1]


# ---------------------------------------------------------------- the script itself

def test_the_script_is_ascii_and_parses():
    SCRIPT.read_bytes().decode("ascii")
    assert b"\r" not in SCRIPT.read_bytes(), "LF line ends: the server's sh reads a CR as part of the command"
    assert subprocess.run([SH, "-n", SCRIPT.as_posix()], capture_output=True).returncode == 0


def test_usage_errors_exit_1(project):
    assert project.run().returncode == 1
    assert project.run("frobnicate").returncode == 1
    assert project.run("deploy").returncode == 1


def test_every_exit_code_is_in_the_runbook():
    runbook = (PROJECT_ROOT / "RELEASE.md").read_text(encoding="utf-8")
    for code in range(6):
        assert re.search(rf"^\| *{code} *\|", runbook, re.M), f"exit code {code} missing from RELEASE.md"


# ---------------------------------------------------------------- build

def git_rules(project, dirty: str = "") -> None:
    project.rule("status --porcelain", 0, dirty)
    project.rule("rev-parse HEAD", 0, "2a8c1e7" + "1" * 33 + "\n")
    project.rule("show -s --date=format-local:%Y.%m.%d --format=%cd HEAD", 0, "2026.11.24\n")
    project.rule("show -s --date=format-local:%Y-%m-%dT%H:%M:%SZ --format=%cd HEAD", 0, "2026-11-24T09:12:00Z\n")
    project.rule("image inspect watch-web:*", 1, "")


def test_build_computes_the_tag_from_the_commit(project):
    git_rules(project)
    result = project.run("build")
    assert result.returncode == 0, result.stdout + result.stderr
    builds = [call for call in project.calls() if call.startswith("build ")]
    assert builds == [
        "build --build-arg WATCH_WEB_VERSION=2026.11.24-2a8c1e7 --build-arg WATCH_WEB_REVISION=2a8c1e7"
        + "1" * 33 + " --build-arg WATCH_WEB_CREATED=2026-11-24T09:12:00Z -t watch-web:2026.11.24-2a8c1e7 ."
    ]
    assert "watch-web:2026.11.24-2a8c1e7" in result.stdout
    env_log = project.calls_with_tag()
    assert any(line.startswith("|show -s --date=format-local:%Y.%m.%d") for line in env_log)


def test_build_takes_the_date_in_utc(project):
    git_rules(project)
    project.rule("show -s --date=format-local:%Y.%m.%d --format=%cd HEAD", 0, "2026.11.24\n")
    result = project.run("build", extra_env={"TZ": "Pacific/Kiritimati"})
    assert result.returncode == 0
    script = SCRIPT.read_text(encoding="ascii")
    assert "TZ=UTC git show -s --date=format-local:%Y.%m.%d --format=%cd HEAD" in script


def test_build_refuses_a_dirty_tree(project):
    git_rules(project, dirty=" M app/main.py\n")
    result = project.run("build")
    assert result.returncode == 2
    assert "uncommitted" in fix_line(result) and "Fix:" in fix_line(result)
    assert not [call for call in project.calls() if call.startswith("build ")]


def test_build_skips_an_existing_tag(project):
    git_rules(project)
    project.rule("image inspect watch-web:2026.11.24-2a8c1e7*", 0, "x\n")
    result = project.run("build")
    assert result.returncode == 0
    assert not [call for call in project.calls() if call.startswith("build ")]
    assert "exists" in result.stdout


# ---------------------------------------------------------------- status

def test_status_prints_tag_services_and_schema(project):
    project.ps(TAG_OLD)
    project.add_history(f"2026-11-20T10:00:00Z deploy 2026.11.01-0000000 -> {TAG_OLD} ok backup=x by=a")
    result = project.run("status")
    assert result.returncode == 0, result.stderr
    out = result.stdout
    assert f"WATCH_WEB_TAG={TAG_OLD}" in out
    assert f"api" in out and f"watch-web:{TAG_OLD}" in out and "healthy" in out
    assert "database_revision=037_outbox_events" in out and "code_head=037_outbox_events" in out
    assert "INFO: Loaded configuration" not in out
    assert "imgproxy image id matches" in out
    assert f"-> {TAG_OLD} ok" in out
    assert not project.changing_calls()


# ---------------------------------------------------------------- deploy: checks before any change

def test_deploy_refuses_an_unknown_image(project):
    project.rule(f"image inspect watch-web:{TAG_NEW} --format*", 1, "Error: No such image\n")
    result = project.run("deploy", TAG_NEW, "--yes")
    assert result.returncode == 2
    assert f"watch-web:{TAG_NEW}" in fix_line(result) and "release.sh load" in fix_line(result)
    assert not project.changing_calls() and project.env == project.env_text


def test_deploy_refuses_when_the_schema_check_refuses(project):
    project.rule("compose run --rm -T --no-deps --entrypoint python migrate -m app.backend.cli.schema_check check*", 3,
                 "SCHEMA CHECK FAILED (migrate): the database is at 040_x, which release y ...\n")
    result = project.run("deploy", TAG_NEW, "--yes")
    assert result.returncode == 3
    assert "SCHEMA CHECK FAILED (migrate)" in result.stdout + result.stderr
    assert not project.changing_calls() and project.env == project.env_text
    assert f"{TAG_NEW}|compose run --rm -T --no-deps --entrypoint python migrate -m app.backend.cli.schema_check " \
           "check --role migrate" in project.calls_with_tag(), "the check runs from the NEW image"


def test_deploy_refuses_without_a_backup_service(project):
    project.rule("compose --profile tools config --services", 0, "api\nmedia-worker\nimgproxy\nmigrate\n")
    result = project.run("deploy", TAG_NEW, "--yes")
    assert result.returncode == 2
    assert "--no-backup" in fix_line(result)
    assert not project.changing_calls() and project.env == project.env_text


def test_deploy_without_a_backup_service_and_no_backup(project):
    project.rule("compose --profile tools config --services", 0, "api\nmedia-worker\nimgproxy\nmigrate\n")
    result = project.run("deploy", TAG_NEW, "--yes", "--no-backup")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not [call for call in project.calls() if "db-backup" in call and "create" in call]
    assert "backup=none" in project.history


def test_deploy_refuses_another_imgproxy_image(project):
    project.rule("image inspect --format {{.Id}} " + IMGPROXY, 0, "sha256:" + "cd" * 32 + "\n")
    result = project.run("deploy", TAG_NEW, "--yes")
    assert result.returncode == 2
    assert "IMGPROXY_IMAGE_ID" in fix_line(result)
    assert not project.changing_calls()


def test_deploy_refuses_too_little_free_space(project):
    result = project.run("deploy", TAG_NEW, "--yes", extra_env={"RELEASE_MIN_FREE_MB": "999999999"})
    assert result.returncode == 2
    assert "free" in fix_line(result)
    assert not project.changing_calls()


def test_deploy_refuses_a_failing_compose_config(project):
    project.rule("compose config -q", 1, "required variable WATCH_WEB_TAG is missing a value\n")
    result = project.run("deploy", TAG_NEW, "--yes")
    assert result.returncode == 2
    assert not project.changing_calls()


# ---------------------------------------------------------------- deploy: the prompt, dry run, steps

def test_no_at_the_prompt_changes_nothing(project):
    result = project.run("deploy", TAG_NEW, answer="n\n")
    assert result.returncode == 4
    assert "Proceed? [y/N]" in result.stdout
    assert not project.changing_calls() and project.env == project.env_text and project.history == ""


def test_an_empty_answer_is_no(project):
    assert project.run("deploy", TAG_NEW, answer="").returncode == 4
    assert not project.changing_calls()


def test_dry_run_prints_the_plan_and_changes_nothing(project):
    result = project.run("deploy", TAG_NEW, "--dry-run")
    assert result.returncode == 0, result.stdout + result.stderr
    out = result.stdout
    for step in ("1. back up the database", "2. migrate the database", "3. set WATCH_WEB_TAG=" + TAG_NEW,
                 "4. restart", "5. wait"):
        assert step in out, step
    assert "Proceed?" not in out
    assert not project.changing_calls() and project.env == project.env_text and project.history == ""
    assert not (project.root / ".env.bak").exists()


def test_a_successful_deploy(project):
    result = project.run("deploy", TAG_NEW, answer="y\n")
    assert result.returncode == 0, result.stdout + result.stderr
    calls = project.calls_with_tag()
    order = [line for line in calls if any(w in line for w in ("db-backup create", "-T migrate", "compose up -d"))]
    assert order == [
        "|compose run --rm -T db-backup create --label pre-upgrade",
        f"{TAG_NEW}|compose run --rm -T migrate",
        "|compose up -d",
    ], "backup with the running release, migrate with the new one, then the switch"
    assert project.env == project.env_text.replace(f"WATCH_WEB_TAG={TAG_OLD}", f"WATCH_WEB_TAG={TAG_NEW}")
    assert (project.root / ".env.bak").read_text() == project.env_text
    assert not (project.root / ".env.tmp").exists()
    line = project.history.strip()
    assert re.fullmatch(rf"\d{{4}}-\d\d-\d\dT\d\d:\d\d:\d\dZ deploy {TAG_OLD} -> {TAG_NEW} ok "
                        r"backup=20261124T101420Z-pre-upgrade by=operator", line), line


def test_deploy_appends_a_missing_tag_line(project):
    text = project.env_text.replace(f"WATCH_WEB_TAG={TAG_OLD}\n", "")
    (project.root / ".env").write_text(text, newline="\n")
    result = project.run("deploy", TAG_NEW, "--yes")
    assert result.returncode == 0, result.stdout + result.stderr
    assert project.env == text + f"WATCH_WEB_TAG={TAG_NEW}\n"
    assert f"deploy none -> {TAG_NEW} ok" in project.history


def test_yes_skips_only_the_prompt(project):
    result = project.run("deploy", TAG_NEW, "--yes")
    assert result.returncode == 0
    assert "Proceed?" not in result.stdout
    assert "1. back up the database" in result.stdout


@pytest.mark.parametrize("failing, step", [
    ("compose run --rm -T db-backup create --label pre-upgrade", "backup"),
    ("compose run --rm -T migrate", "migrate"),
])
def test_a_failing_backup_or_migration_leaves_env_alone(project, failing, step):
    project.rule(failing, 1, "failed: something\n")
    result = project.run("deploy", TAG_NEW, "--yes")
    assert result.returncode == 2
    assert project.env == project.env_text
    assert not [call for call in project.calls() if "compose up" in call]
    assert f"failed step={step}" in project.history


def test_not_healthy_in_time_exits_5_with_the_rollback_hint(project):
    project.ps(TAG_NEW, api_health="starting")
    result = project.run("deploy", TAG_NEW, "--yes")
    assert result.returncode == 5
    assert "deploy/release.sh rollback" in fix_line(result)
    assert f"WATCH_WEB_TAG={TAG_NEW}" in project.env
    assert "failed step=wait" in project.history


def test_waiting_checks_the_running_image(project):
    project.ps(TAG_OLD)  # healthy, but still the old containers
    assert project.run("deploy", TAG_NEW, "--yes").returncode == 5


# ---------------------------------------------------------------- rollback

def test_rollback_picks_the_previous_tag_from_the_history(project):
    project.add_history(f"2026-11-20T10:00:00Z deploy 2026.11.01-0000000 -> {TAG_OLD} ok backup=a by=x",
                        f"2026-11-24T10:00:00Z deploy {TAG_OLD} -> {TAG_NEW} ok backup=20261124T1-pre-upgrade by=x")
    (project.root / ".env").write_text(project.env_text.replace(TAG_OLD, TAG_NEW), newline="\n")
    project.ps(TAG_OLD)
    result = project.run("rollback", "--yes")
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"WATCH_WEB_TAG={TAG_OLD}" in project.env
    assert f"{TAG_OLD}|compose run --rm -T --no-deps --entrypoint python migrate -m app.backend.cli.schema_check " \
           "check --role api" in project.calls_with_tag()
    assert not [call for call in project.calls() if "db-backup" in call or "-T migrate" in call.split("python")[0]]
    assert f"rollback {TAG_NEW} -> {TAG_OLD} ok" in project.history


def test_rollback_refuses_when_the_old_release_refuses_the_schema(project):
    project.add_history(f"2026-11-24T10:00:00Z deploy {TAG_OLD} -> {TAG_NEW} ok backup=20261124T1-pre-upgrade by=x")
    (project.root / ".env").write_text(project.env_text.replace(TAG_OLD, TAG_NEW), newline="\n")
    project.rule("compose run --rm -T --no-deps --entrypoint python migrate -m app.backend.cli.schema_check check*", 3,
                 "SCHEMA CHECK FAILED (api): the database is at 039_c ...\n")
    result = project.run("rollback", "--yes")
    assert result.returncode == 3
    line = fix_line(result)
    assert "database restore" in line and "20261124T1-pre-upgrade" in line and "Restore the database" in line
    assert f"WATCH_WEB_TAG={TAG_NEW}" in project.env
    assert not project.changing_calls()


def test_rollback_without_history_refuses(project):
    result = project.run("rollback", "--yes")
    assert result.returncode == 2
    assert "release-history.log" in fix_line(result)


def test_rollback_to_an_image_older_than_the_schema_check(project):
    """The first release with release.sh rolls back to the build before it, which has no schema_check: the script
    compares that image's alembic head with the database revision instead"""
    project.add_history(f"2026-11-24T10:00:00Z deploy {TAG_OLD} -> {TAG_NEW} ok backup=b by=x")
    (project.root / ".env").write_text(project.env_text.replace(TAG_OLD, TAG_NEW), newline="\n")
    project.rule("compose run --rm -T --no-deps --entrypoint python migrate -m app.backend.cli.schema_check check*", 1,
                 "/usr/local/bin/python: No module named app.backend.cli.schema_check\n")
    project.rule("compose run --rm -T --no-deps --entrypoint python migrate -c *", 0, "037_outbox_events\n")
    project.ps(TAG_OLD)
    result = project.run("rollback", "--yes")
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"WATCH_WEB_TAG={TAG_OLD}" in project.env

    project.rule("compose run --rm -T --no-deps --entrypoint python migrate -c *", 0, "036_webm_media_types\n")
    project.add_history(f"2026-11-24T11:00:00Z deploy {TAG_OLD} -> {TAG_NEW} ok backup=b by=x")
    (project.root / ".env").write_text(project.env_text.replace(TAG_OLD, TAG_NEW), newline="\n")
    assert project.run("rollback", "--yes").returncode == 3


def test_rollback_no_at_the_prompt(project):
    project.add_history(f"2026-11-24T10:00:00Z deploy {TAG_OLD} -> {TAG_NEW} ok backup=b by=x")
    (project.root / ".env").write_text(project.env_text.replace(TAG_OLD, TAG_NEW), newline="\n")
    assert project.run("rollback", answer="no\n").returncode == 4
    assert f"WATCH_WEB_TAG={TAG_NEW}" in project.env


# ---------------------------------------------------------------- prune, save, load

def test_prune_keeps_the_newest_the_env_tag_and_the_history(project):
    tags = ["2026.11.26-0000006", "2026.11.25-0000005", "2026.11.24-0000004", "2026.11.23-0000003",
            "2026.11.22-0000002", TAG_OLD, "2026.11.01-0000001", "<none>"]
    project.rule("image ls watch-web --format {{.Tag}}", 0, "\n".join(tags) + "\n")
    project.add_history("2026-11-22T00:00:00Z deploy 2026.11.01-0000001 -> 2026.11.22-0000002 ok backup=a by=x",
                        "2026-11-23T00:00:00Z deploy 2026.11.22-0000002 -> 2026.11.23-0000003 ok backup=a by=x",
                        "2026-11-24T00:00:00Z rollback 2026.11.23-0000003 -> 2026.11.22-0000002 ok by=x")
    result = project.run("prune", "--keep", "3", "--yes")
    assert result.returncode == 0, result.stdout + result.stderr
    removed = sorted(call.split("watch-web:")[1] for call in project.calls() if call.startswith("image rm"))
    # kept: newest 3 (26, 25, 24), .env (TAG_OLD), the last 3 history lines (01, 22, 23)
    assert removed == [], removed
    project.add_history("2026-11-25T00:00:00Z deploy 2026.11.22-0000002 -> 2026.11.25-0000005 ok backup=a by=x",
                        "2026-11-26T00:00:00Z deploy 2026.11.25-0000005 -> 2026.11.26-0000006 ok backup=a by=x",
                        "2026-11-26T01:00:00Z deploy 2026.11.26-0000006 -> 2026.11.24-0000004 ok backup=a by=x")
    result = project.run("prune", "--keep", "3", "--yes")
    removed = sorted(call.split("watch-web:")[1] for call in project.calls() if call.startswith("image rm"))
    assert removed == ["2026.11.01-0000001", "2026.11.23-0000003"], removed


def test_prune_dry_run_removes_nothing(project):
    project.rule("image ls watch-web --format {{.Tag}}", 0, "a\nb\nc\nd\ne\n")
    result = project.run("prune", "--keep", "1", "--dry-run")
    assert result.returncode == 0
    assert not [call for call in project.calls() if call.startswith("image rm")]
    assert "watch-web:e" in result.stdout


def test_save_writes_the_archive_and_its_checksum(project):
    project.rule(f"save watch-web:{TAG_NEW} {IMGPROXY}", 0, "pretend-image-tar\n")
    result = project.run("save", TAG_NEW, "images.tgz")
    assert result.returncode == 0, result.stdout + result.stderr
    archive = project.root / "images.tgz"
    assert archive.exists() and (project.root / "images.tgz.sha256").exists()
    assert (project.root / "images.tgz.sha256").read_text().split()[1].lstrip("*") == "images.tgz"


def test_load_checks_the_checksum_first(project):
    project.rule(f"save watch-web:{TAG_NEW} {IMGPROXY}", 0, "pretend-image-tar\n")
    assert project.run("save", TAG_NEW, "images.tgz").returncode == 0
    project.rule("load", 0, f"Loaded image: watch-web:{TAG_NEW}\nLoaded image: {IMGPROXY}\n")
    result = project.run("load", "images.tgz")
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"Loaded image: watch-web:{TAG_NEW}" in result.stdout
    assert "load" in project.calls()

    (project.root / "images.tgz").write_bytes(b"tampered")
    calls_before = len([c for c in project.calls() if c == "load"])
    result = project.run("load", "images.tgz")
    assert result.returncode == 2
    assert "checksum" in fix_line(result)
    assert len([c for c in project.calls() if c == "load"]) == calls_before


def test_load_without_the_checksum_file_refuses(project):
    (project.root / "images.tgz").write_bytes(b"x")
    result = project.run("load", "images.tgz")
    assert result.returncode == 2 and ".sha256" in fix_line(result)


# ---------------------------------------------------------------- final review findings

def test_rollback_after_a_failed_deploy_goes_back(project):
    """Review I1: deploy A -> B failed while waiting (exit 5, .env already says B); the Fix line says rollback"""
    project.add_history(f"2026-11-20T10:00:00Z deploy 2026.11.01-0000000 -> {TAG_OLD} ok backup=a by=x",
                        f"2026-11-24T10:00:00Z deploy {TAG_OLD} -> {TAG_NEW} failed step=wait backup=b by=x")
    (project.root / ".env").write_text(project.env_text.replace(TAG_OLD, TAG_NEW), newline="\n")
    project.ps(TAG_OLD)
    result = project.run("rollback", "--yes")
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"WATCH_WEB_TAG={TAG_OLD}" in project.env


def test_rollback_after_a_redeploy_of_the_same_tag(project):
    """Review I1: deploy B again after a fix writes deploy B -> B; rollback still goes to A"""
    project.add_history(f"2026-11-24T10:00:00Z deploy {TAG_OLD} -> {TAG_NEW} failed step=wait backup=b by=x",
                        f"2026-11-24T11:00:00Z deploy {TAG_NEW} -> {TAG_NEW} ok backup=c by=x")
    (project.root / ".env").write_text(project.env_text.replace(TAG_OLD, TAG_NEW), newline="\n")
    project.ps(TAG_OLD)
    result = project.run("rollback", "--yes")
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"WATCH_WEB_TAG={TAG_OLD}" in project.env


def test_rollback_never_migrates(project):
    """Review M1: after a restore and a deploy of the old release, a rollback to the newer release would migrate the
    database forward at its start; a rollback refuses instead"""
    project.add_history(f"2026-11-24T10:00:00Z deploy {TAG_OLD} -> {TAG_NEW} ok backup=b by=x")
    (project.root / ".env").write_text(project.env_text.replace(TAG_OLD, TAG_NEW), newline="\n")
    project.rule("compose run --rm -T --no-deps --entrypoint python migrate -m app.backend.cli.schema_check check*", 0,
                 "Schema check: database at 037_outbox_events; release x migrates it to 038_x\n")
    result = project.run("rollback", "--yes")
    assert result.returncode == 3
    assert "migrate" in fix_line(result) and f"deploy/release.sh deploy {TAG_OLD}" in fix_line(result)
    assert f"WATCH_WEB_TAG={TAG_NEW}" in project.env


def test_save_refuses_when_docker_save_fails(project):
    """Review I2: a pipeline answers gzip's status; a failed docker save must not leave an archive and checksum"""
    project.rule(f"save watch-web:{TAG_NEW} {IMGPROXY}", 1, "")
    result = project.run("save", TAG_NEW, "images.tgz")
    assert result.returncode == 2
    assert "docker save failed" in fix_line(result)
    assert not (project.root / "images.tgz").exists() and not (project.root / "images.tgz.sha256").exists()


def test_save_refuses_a_missing_imgproxy_image(project):
    project.rule("image inspect " + IMGPROXY, 1, "")
    result = project.run("save", TAG_NEW, "images.tgz")
    assert result.returncode == 2 and "docker pull" in fix_line(result)
    assert not [call for call in project.calls() if call.startswith("save ")]


def test_the_script_is_executable_in_git():
    """Review I3: the files archive (git archive) and a checkout must give an executable script"""
    index = subprocess.run(["git", "ls-files", "-s", "deploy/release.sh"], cwd=PROJECT_ROOT, capture_output=True,
                           text=True).stdout
    assert index.startswith("100755 "), index


def test_the_runbook_reads_the_imgproxy_id_on_its_own():
    """Review M4: on an image store where RepoDigests is empty after docker load, a template that indexes it prints
    nothing, the id included; the id is read alone"""
    for name in ("RELEASE.md", ".env.example"):
        text = (PROJECT_ROOT / name).read_text(encoding="utf-8")
        assert "docker image inspect --format '{{.Id}}' ghcr.io/imgproxy/imgproxy:v3.31.4" in text, name
        assert "{{index .RepoDigests 0}} {{.Id}}" not in text, name
