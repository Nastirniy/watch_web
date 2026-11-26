"""GET /database/status and GET /database/backups (spec section 5)"""
from datetime import datetime, timedelta, timezone

import pytest

from app.backend.api.v1 import V1
from app.backend.bootstrap.permissions import permission_level
from app.backend.config import settings
from app.backend.enums.general import BackupLabel, PermissionLevel
from app.backend.enums.routes import DatabaseRoutes
from app.backend.services.backups.manifest import FORMAT_VERSION, Manifest, backup_id, write_manifest
from app.backend.utils.alembic_history import code_head
from app.backend.auth.scope import Node
from app.backend.enums.general import UserType
from tests.helpers import assign, make_admin, make_role, make_user

STATUS = V1.path(DatabaseRoutes.STATUS)
BACKUPS = V1.path(DatabaseRoutes.BACKUPS)


def put(directory, hours_ago: float) -> Manifest:
    started = datetime.now(timezone.utc).replace(microsecond=0) - timedelta(hours=hours_ago)
    manifest = Manifest(
        format_version=FORMAT_VERSION, id=backup_id(started, BackupLabel.SCHEDULED), label=BackupLabel.SCHEDULED,
        started_at=started, finished_at=started + timedelta(minutes=1), database="radio_monitoring",
        server_product="mariadb", server_version="11.8.6", dump_tool="mariadb-dump",
        migration_revision="028_no_actor_foreign_keys", tables=("users",), file="dump.sql.gz", size_bytes=10,
        sha256="ab" * 32,
    )
    (directory / manifest.id).mkdir(parents=True)
    (directory / manifest.id / "dump.sql.gz").write_bytes(b"x")
    write_manifest(directory / manifest.id, manifest)
    return manifest


async def test_status_for_admin(client, db, headers):
    admin = await make_admin(db)
    response = await client.get(STATUS, headers=headers(admin))
    assert response.status_code == 200
    body = response.json()
    assert body["server"]["product"] == "sqlite"
    assert body["server"]["version"]
    assert body["schema"]["code_revision"] == code_head()
    assert "users" in [t["name"] for t in body["schema"]["tables"]]


async def test_status_needs_permission(client, db, headers):
    user = await make_user(db, perms=["statistics.view"])
    response = await client.get(STATUS, headers=headers(user))
    assert response.status_code == 403


async def test_backups_for_admin(client, db, headers, tmp_path, monkeypatch):
    monkeypatch.setattr(settings.backup, "dir", str(tmp_path))
    older, newer = put(tmp_path, 30), put(tmp_path, 2)
    admin = await make_admin(db)
    response = await client.get(BACKUPS, headers=headers(admin))
    assert response.status_code == 200
    body = response.json()
    assert [b["id"] for b in body["backups"]] == [newer.id, older.id]
    assert body["backups"][0]["label"] == "scheduled"
    assert body["backups"][0]["copied"] is None
    assert body["summary"]["count"] == 2
    assert body["summary"]["last_success_id"] == newer.id
    assert body["summary"]["stale"] is False
    assert body["summary"]["copy"] == {"configured": False, "reachable": False, "missing": 0}
    assert body["previous_states"] == []


async def test_backups_not_configured(client, db, headers, monkeypatch):
    monkeypatch.setattr(settings.backup, "dir", "")
    admin = await make_admin(db)
    response = await client.get(BACKUPS, headers=headers(admin))
    assert response.status_code == 503
    assert response.json()["code"] == "backups_not_configured"


async def test_backups_directory_gone(client, db, headers, tmp_path, monkeypatch):
    missing = tmp_path / "share-not-mounted"
    monkeypatch.setattr(settings.backup, "dir", str(missing))
    admin = await make_admin(db)
    response = await client.get(BACKUPS, headers=headers(admin))
    assert response.status_code == 503
    assert response.json()["code"] == "backup_directory_unavailable"
    assert "share-not-mounted" not in response.text


async def test_backups_needs_permission(client, db, headers, tmp_path, monkeypatch):
    monkeypatch.setattr(settings.backup, "dir", str(tmp_path))
    user = await make_user(db, user_type=UserType.SYSTEM_ADMINISTRATOR)
    await assign(db, user, await make_role(db, ["database.view"]), Node.root())
    assert (await client.get(BACKUPS, headers=headers(user))).status_code == 403
    assert (await client.get(STATUS, headers=headers(user))).status_code == 200


@pytest.mark.parametrize("name", ["database.view", "backup.view"])
def test_permissions_are_system_level(name):
    assert permission_level(name) == PermissionLevel.SYSTEM


async def test_a_hung_share_answers_503_without_blocking(client, db, headers, tmp_path, monkeypatch):
    """A hard NFS mount that stops answering: the catalog read times out on its own thread (review)"""
    import threading
    import time
    from app.backend.services.backups import catalog
    monkeypatch.setattr(settings.backup, "dir", str(tmp_path))
    monkeypatch.setattr(catalog, "CATALOG_TIMEOUT_SECONDS", 0.3)
    release = threading.Event()
    real_scan = catalog.scan

    def hung_scan(folder):
        release.wait(10)
        return real_scan(folder)

    monkeypatch.setattr(catalog, "scan", hung_scan)
    admin = await make_admin(db)
    try:
        started = time.monotonic()
        first = await client.get(BACKUPS, headers=headers(admin))
        second = await client.get(BACKUPS, headers=headers(admin))
        assert (first.status_code, second.status_code) == (503, 503)
        assert first.json()["code"] == "backup_directory_unavailable"
        assert time.monotonic() - started < 3
    finally:
        release.set()
