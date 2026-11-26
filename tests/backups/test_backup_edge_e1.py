"""Backups: edge cases of the test plan that need no database server (E1: C6, C11, C12, F3, F4, R8, R13, A2)"""
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.backend.config import BackupConfig, DatabaseConfig
from app.backend.enums.general import BackupLabel
from app.backend.services.backups.manifest import FORMAT_VERSION, Manifest, backup_id, write_manifest
from tests.backup_fakes import Fakes

UTC = timezone.utc
NOW = datetime(2026, 10, 22, 12, 0, 0, tzinfo=UTC)
DB = DatabaseConfig(name="db", user="u", password="p")


def m(started: datetime, label=BackupLabel.SCHEDULED) -> Manifest:
    return Manifest(
        format_version=FORMAT_VERSION, id=backup_id(started, label), label=label, started_at=started,
        finished_at=started, database="db", server_product="mariadb", server_version="11.8", dump_tool="d",
        migration_revision="028_no_actor_foreign_keys", tables=("t",), file="dump.sql.gz", size_bytes=1,
        sha256="00" * 32)


def put(directory: Path, manifest: Manifest, dump: bool = True) -> Path:
    folder = directory / manifest.id
    folder.mkdir(parents=True)
    if dump:
        (folder / "dump.sql.gz").write_bytes(b"x")
    write_manifest(folder, manifest)
    return folder


# ---------------------------------------------------------------- C6: two creates in the same second

def test_c6_a_create_in_the_same_second_gets_its_own_id(tmp_path, monkeypatch):
    from app.backend.services.backups import dump
    monkeypatch.setattr(dump.time, "sleep", lambda seconds: None)
    cfg = BackupConfig(dir=str(tmp_path / "b"))
    tools = Fakes(tmp_path / "bin").tools
    first = dump.create_backup(cfg, DB, BackupLabel.MANUAL, tools=tools, now=lambda: NOW).manifest
    times = iter([NOW] + [NOW + timedelta(seconds=1)] * 50)       # starts in the same second as the first
    second = dump.create_backup(cfg, DB, BackupLabel.MANUAL, tools=tools, now=lambda: next(times)).manifest
    assert first.id != second.id
    assert second.id == backup_id(NOW + timedelta(seconds=1), BackupLabel.MANUAL)


# ---------------------------------------------------------------- C11: retention at the boundaries

def rules(**kw):
    from app.backend.services.backups.retention import RetentionRules
    base = dict(keep_daily=0, keep_weekly=0, keep_monthly=0, keep_manual_days=90)
    return RetentionRules(**{**base, **kw})


def kept(manifests, now, **kw):
    from app.backend.services.backups.retention import select_to_delete
    return {x.id for x in manifests} - set(select_to_delete(manifests, rules(**kw), now))


def test_c11_midnight_utc_separates_days():
    before, after = m(datetime(2026, 10, 21, 23, 59, 59, tzinfo=UTC)), m(datetime(2026, 10, 22, 0, 0, 0, tzinfo=UTC))
    assert kept([before, after], NOW, keep_daily=2) == {before.id, after.id}
    assert kept([before, after], NOW, keep_daily=1) == {after.id}


def test_c11_iso_week_53_and_the_new_year():
    # 2026-12-31 (Thursday) is in ISO week 53 of 2026, 2027-01-04 (Monday) starts week 1 of 2027
    w53, w53_late, w1 = (m(datetime(2026, 12, 28, 3, tzinfo=UTC)), m(datetime(2027, 1, 3, 3, tzinfo=UTC)),
                         m(datetime(2027, 1, 4, 3, tzinfo=UTC)))
    now = datetime(2027, 1, 5, tzinfo=UTC)
    assert kept([w53, w53_late, w1], now, keep_weekly=2) == {w53_late.id, w1.id}


def test_c11_month_end():
    october, november = m(datetime(2026, 10, 31, 23, 0, tzinfo=UTC)), m(datetime(2026, 11, 1, 1, 0, tzinfo=UTC))
    now = datetime(2026, 11, 2, tzinfo=UTC)
    assert kept([october, november], now, keep_monthly=2) == {october.id, november.id}
    assert kept([october, november], now, keep_monthly=1) == {november.id}


def test_c11_everything_zero_keeps_only_the_newest():
    backups = [m(NOW - timedelta(days=d)) for d in range(10)]
    assert kept(backups, NOW) == {backups[0].id}


def test_c11_many_manual_backups():
    backups = [m(NOW - timedelta(hours=h), BackupLabel.MANUAL) for h in range(500)]
    assert len(kept(backups, NOW, keep_manual_days=10)) == 241    # 0..240 hours: the boundary is kept


# ---------------------------------------------------------------- C12: a hand-edited catalog

@pytest.mark.parametrize("key, value", [
    ("migration_revision", 28), ("tables", "users"), ("tables", [1, 2]), ("size_bytes", "big"),
    ("sha256", 123), ("sha256", "short"), ("database", None), ("started_at", "yesterday"),
])
def test_c12_a_manifest_of_the_wrong_type_is_incomplete(tmp_path, key, value):
    from app.backend.services.backups.catalog import load_catalog
    folder = put(tmp_path, m(NOW))
    data = json.loads((folder / "manifest.json").read_text())
    data[key] = value
    (folder / "manifest.json").write_text(json.dumps(data))
    catalog = load_catalog(BackupConfig(dir=str(tmp_path)), NOW)
    assert catalog.entries == () and [i.name for i in catalog.incomplete] == [folder.name]


def test_c12_a_manifest_without_its_dump_is_incomplete(tmp_path):
    from app.backend.services.backups.catalog import load_catalog
    folder = put(tmp_path, m(NOW), dump=False)
    catalog = load_catalog(BackupConfig(dir=str(tmp_path)), NOW)
    assert catalog.entries == () and catalog.incomplete[0].name == folder.name


def test_c12_restore_of_a_backup_without_its_dump_is_refused(tmp_path):
    from app.backend.services.backups.restore import RestoreRefused, plan_restore
    folder = put(tmp_path, m(NOW), dump=False)
    with pytest.raises(RestoreRefused) as exc:
        plan_restore(BackupConfig(dir=str(tmp_path)), DB, folder.name, backend=None)
    assert exc.value.code == "not_found"


@pytest.mark.parametrize("value", [{"error_code": 5}, {"outcome": None}, {"started_at": 1}, []])
def test_c12_a_bad_last_run_is_ignored(tmp_path, value):
    from app.backend.services.backups.catalog import LAST_RUN_FILE, STATE_DIR, read_last_run
    good = {"outcome": "failed", "started_at": NOW.isoformat(), "finished_at": NOW.isoformat(), "error_code": "x",
            "backup_id": None}
    (tmp_path / STATE_DIR).mkdir()
    (tmp_path / STATE_DIR / LAST_RUN_FILE).write_text(json.dumps({**good, **value} if isinstance(value, dict) else value))
    assert read_last_run(tmp_path) is None


def test_c12_a_kept_schema_name_that_is_no_date_is_skipped():
    from app.backend.services.backups.schemas import parse_kept
    rows = [("db_before_20261399_000000", 1), ("db_before_20261022_120000", 2)]
    assert [k.name for k in parse_kept("db", rows)] == ["db_before_20261022_120000"]


async def test_c12_the_api_answers_with_a_hand_edited_catalog(client, db, headers, tmp_path, monkeypatch):
    from app.backend.api.v1 import V1
    from app.backend.config import settings
    from app.backend.enums.routes import DatabaseRoutes
    from tests.helpers import make_admin
    monkeypatch.setattr(settings.backup, "dir", str(tmp_path))
    folder = put(tmp_path, m(NOW))
    data = json.loads((folder / "manifest.json").read_text())
    data["migration_revision"] = 28
    (folder / "manifest.json").write_text(json.dumps(data))
    (tmp_path / ".state").mkdir()
    (tmp_path / ".state" / "last-run.json").write_text(json.dumps({"outcome": "ok", "started_at": NOW.isoformat(),
                                                                   "finished_at": NOW.isoformat(), "error_code": 7,
                                                                   "backup_id": None}))
    response = await client.get(V1.path(DatabaseRoutes.BACKUPS), headers=headers(await make_admin(db)))
    assert response.status_code == 200
    assert response.json()["backups"] == [] and response.json()["summary"]["last_run"] is None


# ---------------------------------------------------------------- F3 / F4: reconcile with odd trees

OLD = NOW.timestamp() - 3600


def touch(path: Path, mtime: float = OLD) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    os.utime(path, (mtime, mtime))
    return path


@pytest.mark.skipif(os.name == "nt", reason="symlinks")
async def test_f3_a_broken_symlink_does_not_stop_the_plan(db, tmp_path):
    from app.backend.services.backups.reconcile import plan_reconcile
    media = tmp_path / "m"
    touch(media / "a/orphan.jpg")
    (media / "a").joinpath("dangling.jpg").symlink_to(tmp_path / "nowhere.jpg")
    report = await plan_reconcile(db, media, None, NOW)
    assert report.orphan_files == ("a/orphan.jpg",)


async def test_f3_cyrillic_and_spaces_in_names(db, tmp_path):
    from app.backend.models.media import MediaFile
    from app.backend.services.backups.reconcile import plan_reconcile
    media = tmp_path / "m"
    db.add(MediaFile(id=1, original_filename="ф.jpg", stored_filename="1_фото з пробілом.jpg",
                     file_path="2026/10/обс 1/1_фото з пробілом.jpg", mime_type="image/jpeg", file_extension="jpg",
                     media_type="image", file_hash="0" * 64, file_size=1))
    await db.commit()
    touch(media / "2026/10/обс 1/1_фото з пробілом.jpg")
    touch(media / "2026/10/обс 2/2_інше.jpg")
    report = await plan_reconcile(db, media, None, NOW)
    assert report.orphan_files == ("2026/10/обс 2/2_інше.jpg",) and report.missing_originals == ()


async def test_f4_hidden_and_nas_folders_are_skipped(db, tmp_path):
    from app.backend.services.backups.reconcile import plan_reconcile
    media = tmp_path / "m"
    for rel in (".snapshot/hourly.0/a.jpg", "2026/@eaDir/a.jpg/SYNOFILE_THUMB_M.jpg", "#recycle/old.jpg",
                "lost+found/123", "2026/10/.tmp-1f2e-copy.jpg", "2026/10/.DS_Store"):
        touch(media / rel)
    touch(media / "2026/10/real_orphan.jpg")
    report = await plan_reconcile(db, media, None, NOW)
    assert report.orphan_files == ("2026/10/real_orphan.jpg",)


# ---------------------------------------------------------------- R8 / R13

def test_r8_a_backup_without_a_revision_is_refused(tmp_path):
    from app.backend.services.backups.restore import RestoreRefused, plan_restore
    manifest = m(NOW)
    folder = put(tmp_path, Manifest(**{**manifest.__dict__, "migration_revision": None}))
    import hashlib
    data = (folder / "dump.sql.gz").read_bytes()
    meta = json.loads((folder / "manifest.json").read_text())
    meta["sha256"] = hashlib.sha256(data).hexdigest()
    (folder / "manifest.json").write_text(json.dumps(meta))
    with pytest.raises(RestoreRefused) as exc:
        plan_restore(BackupConfig(dir=str(tmp_path)), DB, folder.name, backend=None)
    assert exc.value.code == "unknown_revision"


def test_r13_a_kept_schema_of_the_same_second_gets_the_next_name(tmp_path, monkeypatch):
    from app.backend.services.backups import restore
    from tests.backups.test_backup_restore import FakeBackend, put_backup
    from app.backend.services.backups.schemas import KeptSchema, kept_name
    monkeypatch.setattr(restore.time, "sleep", lambda seconds: None)
    cfg = BackupConfig(dir=str(tmp_path / "b"))
    put_backup(Path(cfg.dir))
    taken = kept_name("db", NOW)
    backend = FakeBackend(kept=[KeptSchema(name=taken, created_at=NOW, size_bytes=1)])
    backend.existing.add(taken)
    times = iter([NOW, NOW, NOW, NOW + timedelta(seconds=1)] + [NOW + timedelta(seconds=2)] * 20)
    result = restore.restore_backup(cfg, DB, "latest", backend=backend, now=lambda: next(times),
                                    skip_space_check=True)
    assert result.kept_schema == kept_name("db", NOW + timedelta(seconds=1))


# ---------------------------------------------------------------- A2: a system permission held at a post

@pytest.mark.parametrize("permission", ["database.view", "backup.view"])
def test_a2_database_or_backup_view_never_in_effect_through_a_tree_grant(permission):
    """Authorization v3: database.view / backup.view are system permissions, held only through system roles (no
    node). A tree grant holding one, at a post or at the root, puts it nowhere in effect (auth/integrity.py), whatever
    wrote the row"""
    from app.backend.auth.integrity import effective_permissions, other_level
    from app.backend.enums.general import RoleKind, UserType
    kept, left_out = effective_permissions(UserType.SYSTEM_ADMINISTRATOR, RoleKind.TREE, [permission, "post.view"])
    assert kept == {"post.view"} and left_out == {permission}
    assert other_level(RoleKind.TREE, [permission, "post.view"]) == [permission]
    kept, _ = effective_permissions(UserType.SYSTEM_ADMINISTRATOR, RoleKind.SYSTEM, [permission])
    assert kept == {permission}
