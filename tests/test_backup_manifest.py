"""Backups: the manifest of a backup folder (spec section 2)"""
import json
import os
from datetime import datetime, timezone

import pytest

from app.backend.enums.general import BackupLabel
from app.backend.services.backups.manifest import (
    FORMAT_VERSION, MANIFEST_FILE, Manifest, ManifestError, backup_id, read_manifest, write_manifest,
)

UTC = timezone.utc


def sample(started_at=datetime(2026, 10, 22, 3, 15, 0, tzinfo=UTC), label=BackupLabel.SCHEDULED) -> Manifest:
    return Manifest(
        format_version=FORMAT_VERSION, id=backup_id(started_at, label), label=label, started_at=started_at,
        finished_at=started_at.replace(minute=16), database="radio_monitoring", server_product="mariadb",
        server_version="11.8.6-MariaDB", dump_tool="mariadb-dump from 11.8.6-MariaDB",
        migration_revision="028_no_actor_foreign_keys", tables=("alembic_version", "users"), file="dump.sql.gz",
        size_bytes=1234, sha256="ab" * 32,
    )


def test_backup_id_format():
    assert backup_id(datetime(2026, 10, 22, 3, 15, 0, tzinfo=UTC), BackupLabel.SCHEDULED) == \
        "20261022T031500Z-scheduled"
    assert backup_id(datetime(2026, 10, 22, 3, 15, 0, tzinfo=UTC), BackupLabel.PRE_UPGRADE) == \
        "20261022T031500Z-pre-upgrade"


def test_round_trip(tmp_path):
    manifest = sample()
    folder = tmp_path / manifest.id
    folder.mkdir()
    write_manifest(folder, manifest)
    read = read_manifest(folder)
    assert read == manifest
    assert read.started_at.tzinfo is not None and read.started_at.utcoffset().total_seconds() == 0
    assert not list(folder.glob("*.tmp"))


def _broken(folder, case):
    manifest = sample()
    write_manifest(folder, manifest)
    path = folder / MANIFEST_FILE
    if case == "missing":
        path.unlink()
    elif case == "bad_json":
        path.write_text("{", encoding="utf-8")
    elif case == "missing_key":
        data = json.loads(path.read_text(encoding="utf-8"))
        del data["sha256"]
        path.write_text(json.dumps(data), encoding="utf-8")
    elif case == "format_version":
        data = json.loads(path.read_text(encoding="utf-8"))
        data["format_version"] = 2
        path.write_text(json.dumps(data), encoding="utf-8")
    elif case == "other_id":
        data = json.loads(path.read_text(encoding="utf-8"))
        data["id"] = "20261021T031500Z-scheduled"
        path.write_text(json.dumps(data), encoding="utf-8")


@pytest.mark.parametrize("case", ["missing", "bad_json", "missing_key", "format_version", "other_id"])
def test_unreadable_manifests(tmp_path, case):
    folder = tmp_path / sample().id
    folder.mkdir()
    _broken(folder, case)
    with pytest.raises(ManifestError) as exc:
        read_manifest(folder)
    assert exc.value.reason


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_manifest_mode(tmp_path):
    manifest = sample()
    folder = tmp_path / manifest.id
    folder.mkdir()
    write_manifest(folder, manifest)
    assert (folder / MANIFEST_FILE).stat().st_mode & 0o777 == 0o600
