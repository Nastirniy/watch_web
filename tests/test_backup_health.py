"""/health: the backups part (spec section 5), and the start check of the backup settings"""
from datetime import datetime, timedelta, timezone

import pytest

from app.backend.bootstrap.backups import check_backup_config
from app.backend.config import BackupConfig, settings
from app.backend.enums.general import BackupLabel
from app.backend.enums.routes import SystemRoutes
from app.backend.services.backups.catalog import LastRun, write_last_run
from app.backend.services.backups.manifest import FORMAT_VERSION, Manifest, backup_id, write_manifest


@pytest.fixture(autouse=True)
def no_rate_limit(monkeypatch):
    monkeypatch.setattr(settings.health, "enable_rate_limit", False)


def put(directory, hours_ago: float, sha="ab" * 32) -> Manifest:
    started = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=hours_ago)
    manifest = Manifest(
        format_version=FORMAT_VERSION, id=backup_id(started, BackupLabel.SCHEDULED), label=BackupLabel.SCHEDULED,
        started_at=started, finished_at=started, database="db", server_product="mariadb", server_version="11.8",
        dump_tool="d", migration_revision="028", tables=("t",), file="dump.sql.gz", size_bytes=1, sha256=sha,
    )
    (directory / manifest.id).mkdir(parents=True)
    (directory / manifest.id / "dump.sql.gz").write_bytes(b"x")
    write_manifest(directory / manifest.id, manifest)
    return manifest


@pytest.fixture
def backups(tmp_path, monkeypatch):
    directory = tmp_path / "backups"
    directory.mkdir()
    monkeypatch.setattr(settings.backup, "dir", str(directory))
    return directory


async def health(client):
    response = await client.get(SystemRoutes.HEALTH.value)
    assert response.status_code == 200
    return response.json()


async def test_no_backups_part_without_configuration(client, db, monkeypatch):
    monkeypatch.setattr(settings.backup, "dir", "")
    assert (await health(client))["backups"] is None


async def test_healthy_with_fresh_backup(client, db, backups):
    newest = put(backups, 1)
    body = await health(client)
    assert body["backups"]["status"] == "healthy"
    assert body["backups"]["stale"] is False
    assert body["backups"]["last_success_at"].startswith(newest.finished_at.strftime("%Y-%m-%dT%H:%M"))
    assert body["status"] == "healthy"


async def test_degraded_when_stale(client, db, backups):
    put(backups, 30)
    body = await health(client)
    assert body["backups"]["status"] == "degraded" and body["backups"]["stale"] is True
    assert body["status"] == "degraded"


async def test_degraded_when_last_run_failed(client, db, backups):
    put(backups, 1)
    now = datetime.now(timezone.utc)
    write_last_run(backups, LastRun(outcome="failed", started_at=now, finished_at=now, error_code="no_space",
                                    backup_id=None))
    body = await health(client)
    assert body["backups"]["last_run_outcome"] == "failed"
    assert body["backups"]["status"] == "degraded" and body["status"] == "degraded"


async def test_degraded_when_newest_not_copied(client, db, backups, tmp_path, monkeypatch):
    share = tmp_path / "share"
    share.mkdir()
    monkeypatch.setattr(settings.backup, "copy_dir", str(share))
    put(backups, 1)
    assert (await health(client))["backups"]["status"] == "degraded"


async def test_healthy_when_newest_copied(client, db, backups, tmp_path, monkeypatch):
    share = tmp_path / "share"
    monkeypatch.setattr(settings.backup, "copy_dir", str(share))
    put(backups, 1)
    put(share, 1)
    assert (await health(client))["backups"]["status"] == "healthy"


async def test_degraded_when_directory_gone(client, db, tmp_path, monkeypatch):
    monkeypatch.setattr(settings.backup, "dir", str(tmp_path / "not-mounted"))
    body = await health(client)
    assert body["backups"] == {"status": "degraded", "last_success_at": None, "stale": True,
                               "last_run_outcome": None}
    assert body["status"] == "degraded"


def test_start_check(tmp_path):
    check_backup_config(BackupConfig())
    (tmp_path / "b").mkdir()
    check_backup_config(BackupConfig(dir=str(tmp_path / "b")))
    with pytest.raises(RuntimeError, match="backup.dir"):
        check_backup_config(BackupConfig(dir=str(tmp_path / "missing")))
    with pytest.raises(RuntimeError, match="backup.copy_dir"):
        check_backup_config(BackupConfig(dir=str(tmp_path / "b"), copy_dir=str(tmp_path / "missing")))
