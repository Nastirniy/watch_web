"""Deployment files of the backups: the db-backup tool service, the API's read-only mounts, the systemd units"""
import re
from pathlib import Path

import yaml

from app.backend.config import BackupConfig

ROOT = Path(__file__).resolve().parent.parent
BASE_TEXT = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
BACKUP_TEXT = (ROOT / "docker-compose.backup.yml").read_text(encoding="utf-8")
BACKUP = yaml.safe_load(BACKUP_TEXT)["services"]
COPY = yaml.safe_load((ROOT / "docker-compose.backup-copy.yml").read_text(encoding="utf-8"))["services"]
SYSTEMD = ROOT / "deploy" / "systemd"


def test_tool_service_only_on_demand():
    tool = BACKUP["db-backup"]
    assert tool["profiles"] == ["tools"]
    assert tool["image"] == "watch-web" and tool["pull_policy"] == "never"
    assert tool["network_mode"] == "host"
    assert "ports" not in tool and "restart" not in tool
    assert tool["entrypoint"][-1] == "app.backend.cli.db_backup"
    assert tool["env_file"] == ".env"


def test_backup_dir_writable_for_tool_readonly_for_api():
    assert BACKUP["db-backup"]["volumes"] == ["${BACKUP_DIR}:${BACKUP_DIR}"]
    assert BACKUP["api"]["volumes"] == ["${BACKUP_DIR}:${BACKUP_DIR}:ro"]
    for service in ("db-backup", "api"):
        assert BACKUP[service]["environment"]["MYSETTINGS_BACKUP__DIR"] == "${BACKUP_DIR:?set BACKUP_DIR in .env}"


def test_tool_runs_as_its_own_user_and_the_api_joins_its_group():
    """Backups hold password and token hashes: never MEDIA_UID / MEDIA_GID, which may be the nginx user
    (www-data); the api reads the manifests through BACKUP_GID"""
    assert BACKUP["db-backup"]["user"] == "${BACKUP_UID:?set BACKUP_UID in .env}:${BACKUP_GID:?set BACKUP_GID in .env}"
    assert BACKUP["api"]["group_add"] == ["${BACKUP_GID:?set BACKUP_GID in .env}"]
    assert "MEDIA_UID" not in BACKUP["db-backup"]["user"] and "MEDIA_GID" not in BACKUP["db-backup"]["user"]


def test_no_media_mount_on_tool():
    assert not any("MEDIA_ROOT" in volume for volume in BACKUP["db-backup"]["volumes"])


def test_copy_override():
    assert COPY["db-backup"]["volumes"] == ["${BACKUP_COPY_DIR}:${BACKUP_COPY_DIR}"]
    assert COPY["api"]["volumes"] == ["${BACKUP_COPY_DIR}:${BACKUP_COPY_DIR}:ro"]
    for service in ("db-backup", "api"):
        assert COPY[service]["environment"]["MYSETTINGS_BACKUP__COPY_DIR"] == \
            "${BACKUP_COPY_DIR:?set BACKUP_COPY_DIR in .env}"
    assert "BACKUP_COPY_DIR" not in BACKUP_TEXT


def test_base_compose_names_the_override():
    assert "docker-compose.backup.yml" in BASE_TEXT
    assert "BACKUP_DIR" not in BASE_TEXT


def test_timer_command():
    service = (SYSTEMD / "watch-web-db-backup.service").read_text(encoding="utf-8")
    assert "ExecStart=/usr/bin/docker compose run --rm -T db-backup create --label scheduled" in service
    assert "Type=oneshot" in service
    timer = (SYSTEMD / "watch-web-db-backup.timer").read_text(encoding="utf-8")
    for line in ("OnCalendar=*-*-* 03:15", "Persistent=true", "RandomizedDelaySec=15m"):
        assert re.search(rf"^{re.escape(line)}$", timer, re.MULTILINE), line


def test_env_md_lists_backup_settings():
    env = (ROOT / "ENV.md").read_text(encoding="utf-8")
    for name in BackupConfig.model_fields:
        assert f"MYSETTINGS_BACKUP__{name.upper()}" in env, name


def test_env_example_names_the_backup_variables():
    example = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "BACKUP_DIR=" in example and "BACKUP_COPY_DIR=" in example
    assert "BACKUP_UID=" in example and "BACKUP_GID=" in example
    assert "docker-compose.backup.yml" in example
