"""Backups: create (mariadb-dump), the lock, the off-host copy and prune (spec sections 2 and 3)"""
import gzip
import hashlib
import os
import shutil
import time
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.backend.config import BackupConfig, DatabaseConfig
from app.backend.enums.general import BackupLabel
from app.backend.services.backups import dump as dump_module
from app.backend.services.backups.catalog import load_catalog, read_last_run
from app.backend.services.backups.dump import create_backup, prune
from app.backend.services.backups.failures import BackupFailure
from app.backend.services.backups.locking import writer_lock
from app.backend.services.backups.manifest import FORMAT_VERSION, Manifest, backup_id, read_manifest, write_manifest
from tests.backup_fakes import Fakes

UTC = timezone.utc
PASSWORD = "s3cret-pw-quote\"back\\slash"
DB = DatabaseConfig(host="127.0.0.1", port=3306, name="radio_monitoring", user="radio_user", password=PASSWORD)


@pytest.fixture
def fakes(tmp_path) -> Fakes:
    return Fakes(tmp_path / "bin")


@pytest.fixture
def cfg(tmp_path) -> BackupConfig:
    return BackupConfig(dir=str(tmp_path / "backups"))


def clock(start=datetime(2026, 10, 22, 3, 15, 0, tzinfo=UTC)):
    times = iter(start + timedelta(seconds=i) for i in range(1000))
    return lambda: next(times)


def run(cfg, fakes, label=BackupLabel.MANUAL, **kw):
    return create_backup(cfg, DB, label, tools=fakes.tools, now=kw.pop("now", clock()), **kw)


def folders(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if p.name != ".state")


def test_create_writes_folder_and_manifest(cfg, fakes):
    result = run(cfg, fakes)
    manifest = result.manifest
    assert manifest.id == "20261022T031500Z-manual"
    folder = Path(cfg.dir) / manifest.id
    assert read_manifest(folder) == manifest
    data = (folder / "dump.sql.gz").read_bytes()
    assert gzip.decompress(data).decode("utf-8").endswith("VALUES ('028_no_actor_foreign_keys');\n")
    assert manifest.tables == ("a", "alembic_version")
    assert manifest.migration_revision == "028_no_actor_foreign_keys"
    assert manifest.server_version == "11.8.6-MariaDB-0ubuntu0.26.04.1"
    assert manifest.server_product == "mariadb"
    assert manifest.dump_tool.startswith("mariadb-dump from 11.8.6-MariaDB")
    assert manifest.database == "radio_monitoring"
    assert manifest.sha256 == hashlib.sha256(data).hexdigest()
    assert manifest.size_bytes == len(data)
    assert read_last_run(Path(cfg.dir)).outcome == "ok"
    assert read_last_run(Path(cfg.dir)).backup_id == manifest.id
    assert folders(Path(cfg.dir)) == [manifest.id]


def test_dump_arguments(cfg, fakes):
    run(cfg, fakes)
    (record,) = fakes.dump.records()
    assert record["argv"][0].startswith("--defaults-extra-file=")
    assert record["argv"][1:] == ["--single-transaction", "--quick", "--hex-blob", "--default-character-set=utf8mb4",
                                  "--no-tablespaces", "--skip-lock-tables", "radio_monitoring"]


def test_password_never_in_argv_or_env(cfg, fakes):
    run(cfg, fakes)
    (record,) = fakes.dump.records()
    assert not any("s3cret" in arg for arg in record["argv"])
    assert not any("s3cret" in value for value in record["env"].values())
    assert "s3cret" in record["defaults"] and 'user="radio_user"' in record["defaults"]
    defaults_path = Path(record["argv"][0].split("=", 1)[1])
    assert not defaults_path.exists() and not defaults_path.parent.exists()


def test_failure_leaves_no_backup(cfg, fakes):
    fakes.dump.configure(body="-- partial\n", exit_code=2, stderr="mariadb-dump: Got error: 1045")
    with pytest.raises(BackupFailure) as exc:
        run(cfg, fakes)
    assert exc.value.code == "dump_failed" and exc.value.refused is False
    assert folders(Path(cfg.dir)) == []
    last = read_last_run(Path(cfg.dir))
    assert last.outcome == "failed" and last.error_code == "dump_failed" and last.backup_id is None
    defaults_path = Path(fakes.dump.records()[0]["argv"][0].split("=", 1)[1])
    assert not defaults_path.exists()


def test_timeout(tmp_path, fakes):
    cfg = BackupConfig(dir=str(tmp_path / "backups"), dump_timeout_seconds=1)
    fakes.dump.configure(body="x", sleep=3)
    with pytest.raises(BackupFailure) as exc:
        run(cfg, fakes)
    assert exc.value.code == "timeout"
    assert folders(Path(cfg.dir)) == []
    assert read_last_run(Path(cfg.dir)).error_code == "timeout"


def test_old_tmp_removed_young_kept(cfg, fakes):
    directory = Path(cfg.dir)
    directory.mkdir()
    old, young = directory / ".tmp-20261021T000000Z-manual", directory / ".tmp-20261022T031000Z-manual"
    old.mkdir()
    young.mkdir()
    long_ago = time.time() - cfg.dump_timeout_seconds - 60
    os.utime(old, (long_ago, long_ago))
    run(cfg, fakes)
    assert not old.exists() and young.exists()


def test_no_space_refused(cfg, fakes, monkeypatch):
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda path: usage(2 ** 30, 2 ** 30, 10 * 2 ** 20))
    with pytest.raises(BackupFailure) as exc:
        run(cfg, fakes)
    assert exc.value.code == "no_space" and exc.value.refused is True
    assert fakes.dump.records() == []
    assert read_last_run(Path(cfg.dir)).error_code == "no_space"


def test_space_needed_grows_with_the_last_backup(cfg, fakes, monkeypatch):
    """1.5 x the last backup plus min_free_mb"""
    first = run(cfg, fakes).manifest
    usage = namedtuple("usage", "total used free")
    need = int(first.size_bytes * 1.5) + cfg.min_free_mb * 2 ** 20
    monkeypatch.setattr(shutil, "disk_usage", lambda path: usage(2 ** 40, 0, need - 1))
    with pytest.raises(BackupFailure) as exc:
        run(cfg, fakes, now=clock(datetime(2026, 10, 22, 4, 0, 0, tzinfo=UTC)))
    assert exc.value.code == "no_space"


def test_unsupported_objects_refused(cfg, fakes):
    with pytest.raises(BackupFailure) as exc:
        run(cfg, fakes, object_guard=lambda: ["view v1"])
    assert exc.value.code == "unsupported_objects" and exc.value.refused is True
    assert fakes.dump.records() == []


def test_lock_busy(cfg, fakes):
    directory = Path(cfg.dir)
    directory.mkdir()
    with writer_lock(directory):
        with pytest.raises(BackupFailure) as exc:
            run(cfg, fakes)
    assert exc.value.code == "busy" and exc.value.refused is True
    assert fakes.dump.records() == []
    run(cfg, fakes)  # released


def test_copy_and_verify(tmp_path, fakes):
    cfg = BackupConfig(dir=str(tmp_path / "backups"), copy_dir=str(tmp_path / "share"))
    (tmp_path / "share").mkdir()
    result = run(cfg, fakes)
    assert result.copied is True
    assert read_manifest(tmp_path / "share" / result.manifest.id) == result.manifest
    catalog = load_catalog(cfg, datetime(2026, 10, 22, 4, 0, 0, tzinfo=UTC))
    assert catalog.entries[0].copied is True


def test_copy_corrupted_fails_but_keeps_local(tmp_path, fakes, monkeypatch):
    cfg = BackupConfig(dir=str(tmp_path / "backups"), copy_dir=str(tmp_path / "share"))
    (tmp_path / "share").mkdir()
    real_copy = dump_module._copy_file

    def corrupting(src: Path, dst: Path) -> None:
        real_copy(src, dst)
        if dst.name.endswith(".gz"):
            dst.write_bytes(b"broken")

    monkeypatch.setattr(dump_module, "_copy_file", corrupting)
    with pytest.raises(BackupFailure) as exc:
        run(cfg, fakes)
    assert exc.value.code == "copy_failed"
    catalog = load_catalog(cfg, datetime(2026, 10, 22, 4, 0, 0, tzinfo=UTC))
    assert len(catalog.entries) == 1 and catalog.entries[0].copied is False
    assert folders(tmp_path / "share") == []
    assert read_last_run(Path(cfg.dir)).error_code == "copy_failed"


def put(directory: Path, started: datetime, label=BackupLabel.SCHEDULED) -> str:
    manifest = Manifest(
        format_version=FORMAT_VERSION, id=backup_id(started, label), label=label, started_at=started,
        finished_at=started, database="db", server_product="mariadb", server_version="11.8", dump_tool="d",
        migration_revision="028", tables=("t",), file="dump.sql.gz", size_bytes=1, sha256="00" * 32,
    )
    folder = directory / manifest.id
    folder.mkdir(parents=True)
    write_manifest(folder, manifest)
    return manifest.id


def test_prune_after_create(tmp_path, fakes):
    cfg = BackupConfig(dir=str(tmp_path / "backups"), copy_dir=str(tmp_path / "share"), keep_daily=2,
                       keep_weekly=0, keep_monthly=0)
    start = datetime(2026, 10, 22, 3, 15, 0, tzinfo=UTC)
    old = [put(Path(cfg.dir), start - timedelta(days=d)) for d in (1, 2, 3)]
    old_copies = [put(tmp_path / "share", start - timedelta(days=d)) for d in (1, 2, 3)]
    result = create_backup(cfg, DB, BackupLabel.SCHEDULED, tools=fakes.tools, now=clock(start))
    assert set(result.pruned) == set(old[1:])
    assert folders(Path(cfg.dir)) == sorted([result.manifest.id, old[0]])
    assert folders(tmp_path / "share") == sorted([result.manifest.id, old_copies[0]])


def test_prune_dry_run_deletes_nothing(tmp_path):
    cfg = BackupConfig(dir=str(tmp_path / "backups"), keep_daily=1, keep_weekly=0, keep_monthly=0)
    start = datetime(2026, 10, 22, 3, 15, 0, tzinfo=UTC)
    ids = [put(Path(cfg.dir), start - timedelta(days=d)) for d in (0, 1)]
    assert prune(cfg, start, dry_run=True) == [ids[1]]
    assert folders(Path(cfg.dir)) == sorted(ids)
    assert prune(cfg, start) == [ids[1]]
    assert folders(Path(cfg.dir)) == [ids[0]]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_modes(cfg, fakes):
    os.umask(0o022)
    result = run(cfg, fakes)
    folder = Path(cfg.dir) / result.manifest.id
    assert folder.stat().st_mode & 0o777 == 0o700
    assert (folder / "dump.sql.gz").stat().st_mode & 0o777 == 0o600
    assert (folder / "manifest.json").stat().st_mode & 0o777 == 0o600
    assert Path(cfg.dir).stat().st_mode & 0o777 == 0o700
