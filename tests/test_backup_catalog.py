"""Backups: the catalog of backup folders and its summary (spec sections 2 and 5)"""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app.backend.config import BackupConfig
from app.backend.enums.errors import ErrorCode
from app.backend.enums.general import BackupLabel
from app.backend.errors.coded import CodedError
from app.backend.services.backups.catalog import (
    LastRun, load_catalog, read_last_run, scan, write_last_run,
)
from app.backend.services.backups.manifest import FORMAT_VERSION, Manifest, backup_id, write_manifest

UTC = timezone.utc
NOW = datetime(2026, 10, 22, 12, 0, 0, tzinfo=UTC)


def put(directory, hours_ago: float, size: int = 100, sha: str = "aa" * 32,
        label: BackupLabel = BackupLabel.SCHEDULED) -> Manifest:
    started = NOW - timedelta(hours=hours_ago)
    manifest = Manifest(
        format_version=FORMAT_VERSION, id=backup_id(started, label), label=label, started_at=started,
        finished_at=started + timedelta(minutes=2), database="db", server_product="mariadb", server_version="11.8",
        dump_tool="mariadb-dump", migration_revision="028", tables=("t",), file="dump.sql.gz", size_bytes=size,
        sha256=sha,
    )
    folder = directory / manifest.id
    folder.mkdir(parents=True)
    (folder / "dump.sql.gz").write_bytes(b"x" * size)
    write_manifest(folder, manifest)
    return manifest


def cfg(tmp_path, **kw) -> BackupConfig:
    return BackupConfig(dir=str(tmp_path / "b"), **kw)


@pytest.fixture
def bdir(tmp_path):
    d = tmp_path / "b"
    d.mkdir()
    return d


def test_entries_newest_first_with_totals(tmp_path, bdir):
    a, b, c = put(bdir, 50, 10), put(bdir, 2, 20), put(bdir, 26.5, 30)
    catalog = load_catalog(cfg(tmp_path), NOW)
    assert [e.manifest.id for e in catalog.entries] == [b.id, c.id, a.id]
    assert catalog.summary.count == 3
    assert catalog.summary.total_size_bytes == 60
    assert catalog.summary.last_success_id == b.id
    assert catalog.summary.age_seconds == 2 * 3600 - 120
    assert catalog.summary.free_bytes > 0
    assert all(e.copied is None for e in catalog.entries)
    assert catalog.summary.copy.configured is False


def test_damaged_manifest_is_incomplete(tmp_path, bdir):
    good = put(bdir, 1)
    bad = put(bdir, 2)
    (bdir / bad.id / "manifest.json").write_text("{", encoding="utf-8")
    catalog = load_catalog(cfg(tmp_path), NOW)
    assert [e.manifest.id for e in catalog.entries] == [good.id]
    assert [i.name for i in catalog.incomplete] == [bad.id]
    assert catalog.incomplete[0].reason


def test_tmp_and_state_ignored(tmp_path, bdir):
    (bdir / ".tmp-20261022T091000Z-manual").mkdir()
    (bdir / ".state").mkdir()
    (bdir / "stray.txt").write_text("x")
    manifests, incomplete = scan(bdir)
    assert manifests == [] and incomplete == []


@pytest.mark.parametrize("hours_ago, stale", [(25, False), (27, True)])
def test_stale(tmp_path, bdir, hours_ago, stale):
    put(bdir, hours_ago)
    assert load_catalog(cfg(tmp_path, stale_after_hours=26), NOW).summary.stale is stale


def test_stale_without_any_backup(tmp_path, bdir):
    summary = load_catalog(cfg(tmp_path), NOW).summary
    assert summary.stale is True
    assert summary.last_success_at is None and summary.age_seconds is None and summary.count == 0


def test_copied_by_checksum(tmp_path, bdir):
    copy = tmp_path / "c"
    same, other, missing = put(bdir, 1), put(bdir, 2), put(bdir, 3)
    put(copy, 1)
    put(copy, 2, sha="bb" * 32)
    catalog = load_catalog(cfg(tmp_path, copy_dir=str(copy)), NOW)
    copied = {e.manifest.id: e.copied for e in catalog.entries}
    assert copied == {same.id: True, other.id: False, missing.id: False}
    assert catalog.summary.copy.configured is True
    assert catalog.summary.copy.reachable is True
    assert catalog.summary.copy.missing == 2


def test_copy_dir_unreachable(tmp_path, bdir):
    put(bdir, 1)
    catalog = load_catalog(cfg(tmp_path, copy_dir=str(tmp_path / "no-share")), NOW)
    assert catalog.summary.copy.reachable is False
    assert all(e.copied is None for e in catalog.entries)


def test_last_run_round_trip(tmp_path, bdir):
    run = LastRun(outcome="failed", started_at=NOW, finished_at=NOW + timedelta(seconds=5),
                  error_code="dump_failed", backup_id=None)
    write_last_run(bdir, run)
    assert read_last_run(bdir) == run
    assert load_catalog(cfg(tmp_path), NOW).summary.last_run == run
    write_last_run(bdir, replace(run, outcome="ok", error_code=None, backup_id="x"))
    assert read_last_run(bdir).outcome == "ok"


def test_last_run_unreadable_is_none(tmp_path, bdir):
    assert read_last_run(bdir) is None
    (bdir / ".state").mkdir()
    (bdir / ".state" / "last-run.json").write_text("[", encoding="utf-8")
    assert read_last_run(bdir) is None


def test_not_configured():
    with pytest.raises(CodedError) as exc:
        load_catalog(BackupConfig(), NOW)
    assert exc.value.code == ErrorCode.BACKUPS_NOT_CONFIGURED


def test_directory_missing(tmp_path):
    with pytest.raises(CodedError) as exc:
        load_catalog(cfg(tmp_path), NOW)
    assert exc.value.code == ErrorCode.BACKUP_DIRECTORY_UNAVAILABLE
