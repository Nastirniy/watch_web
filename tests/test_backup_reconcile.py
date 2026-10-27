"""restore_reconcile: media and PKI files against the restored rows (spec section 8.3)"""
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlmodel import select

from app.backend.cli.restore_reconcile import run
from app.backend.models.audit import AuditLog
from app.backend.models.media import MediaFile
from app.backend.models.user_certificate import UserCertificate
from app.backend.services.backups.reconcile import apply_reconcile, plan_reconcile
from tests.helpers import make_user

NOW = datetime(2026, 10, 22, 12, 0, 0, tzinfo=timezone.utc)
STAMP = "20261022T120000Z"
OLD = NOW.timestamp() - 3600


def touch(path: Path, mtime: float = OLD, content: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    os.utime(path, (mtime, mtime))
    return path


async def media_row(db, n: int, rel: str, is_deleted: bool = False, media_type: str = "image") -> MediaFile:
    row = MediaFile(id=n, original_filename=f"f{n}.jpg", stored_filename=Path(rel).name, file_path=rel,
                    mime_type="image/jpeg", file_extension="jpg", media_type=media_type, file_hash=f"{n:064d}",
                    file_size=1, is_deleted=is_deleted)
    db.add(row)
    await db.commit()
    return row


@pytest.fixture
def media(tmp_path) -> Path:
    root = tmp_path / "uploads"
    root.mkdir()
    return root


@pytest.fixture
def store(tmp_path) -> Path:
    root = tmp_path / "issued"
    root.mkdir()
    return root


async def test_orphan_file_found_and_moved(db, media):
    await media_row(db, 1, "2026/10/20/obs_1/1_aa.jpg")
    touch(media / "2026/10/20/obs_1/1_aa.jpg")
    touch(media / "2026/10/21/obs_9/9_bb.jpg")
    report = await plan_reconcile(db, media, None, NOW)
    assert report.orphan_files == ("2026/10/21/obs_9/9_bb.jpg",)
    done = apply_reconcile(report, media, None, NOW)
    assert done.orphan_files == ("2026/10/21/obs_9/9_bb.jpg",) and done.failed == ()
    assert (media / "_orphaned" / STAMP / "2026/10/21/obs_9/9_bb.jpg").read_bytes() == b"x"
    assert not (media / "2026/10/21/obs_9/9_bb.jpg").exists()
    assert (media / "2026/10/20/obs_1/1_aa.jpg").exists()


async def test_archived_row_keeps_its_file(db, media):
    await media_row(db, 2, "a/2_x.jpg", is_deleted=True)
    touch(media / "a/2_x.jpg")
    assert (await plan_reconcile(db, media, None, NOW)).orphan_files == ()


async def test_young_file_and_tmp_skipped(db, media):
    touch(media / "b/young.jpg", mtime=NOW.timestamp() - 60)
    touch(media / "_tmp/uploads/part.bin")
    touch(media / "_orphaned/20261001T000000Z/c/old.jpg")
    assert (await plan_reconcile(db, media, None, NOW)).orphan_files == ()


async def test_derived_without_row_deleted_with_row_kept(db, media):
    await media_row(db, 5, "v/5_a.mp4", media_type="video")
    touch(media / "v/5_a.mp4")
    touch(media / "_derived/posters/000/5.jpg")
    touch(media / "_derived/posters/000/7.jpg")
    touch(media / "_derived/storyboards/000/7/sheet-001.jpg")
    touch(media / "_derived/storyboards/000/5/sheet-001.jpg")
    (media / "_derived/storyboards/000/.tmp-8").mkdir()
    for folder in ("5", "7"):     # a storyboard folder is as old as its last change
        os.utime(media / "_derived/storyboards/000" / folder, (OLD, OLD))
    report = await plan_reconcile(db, media, None, NOW)
    assert set(report.stale_derived) == {"_derived/posters/000/7.jpg", "_derived/storyboards/000/7"}
    apply_reconcile(report, media, None, NOW)
    assert not (media / "_derived/posters/000/7.jpg").exists()
    assert not (media / "_derived/storyboards/000/7").exists()
    assert (media / "_derived/posters/000/5.jpg").exists() and (media / "_derived/storyboards/000/5").exists()
    assert (media / "_derived/storyboards/000/.tmp-8").exists()


async def test_missing_original_reported(db, media):
    await media_row(db, 3, "c/3_gone.jpg")
    report = await plan_reconcile(db, media, None, NOW)
    assert report.missing_originals == (3,) and report.returned_originals == ()


async def test_missing_original_moved_back_from_orphaned(db, media):
    """An undo after an --apply: the moved file has its row again"""
    await media_row(db, 4, "d/4_back.jpg")
    touch(media / "_orphaned/20261022T100000Z/d/4_back.jpg", content=b"original")
    report = await plan_reconcile(db, media, None, NOW)
    assert report.returned_originals == ("_orphaned/20261022T100000Z/d/4_back.jpg",)
    assert report.missing_originals == ()
    apply_reconcile(report, media, None, NOW)
    assert (media / "d/4_back.jpg").read_bytes() == b"original"
    assert not (media / "_orphaned/20261022T100000Z/d/4_back.jpg").exists()


async def certificate_row(db, user, cert_id: int, files_dir):
    db.add(UserCertificate(id=cert_id, user_id=user.id, source="issued", label=f"c{cert_id}",
                           fingerprint_hash=f"fp{cert_id}", cn_hash="cn", cn_hash_scheme=2, serial_hash=f"s{cert_id}",
                           files_dir=files_dir))
    await db.commit()


async def test_orphan_package_quarantined(db, media, store):
    user = await make_user(db)
    await certificate_row(db, user, 10, f"{user.id}/10")
    touch(store / f"{user.id}/10/key.p12")
    package = touch(store / "77/11/key.p12", content=b"secret")
    os.utime(package.parent, (OLD, OLD))
    if os.name != "nt":
        package.chmod(0o600)
    touch(store / f"{user.id}/12/key.p12", mtime=NOW.timestamp() - 30)   # being issued right now
    os.utime(store / f"{user.id}/12", (NOW.timestamp() - 30, NOW.timestamp() - 30))
    report = await plan_reconcile(db, media, store, NOW)
    assert report.orphan_packages == ("77/11",)
    apply_reconcile(report, media, store, NOW)
    moved = store / ".quarantine" / STAMP / "77/11/key.p12"
    assert moved.read_bytes() == b"secret" and not (store / "77/11").exists()
    if os.name != "nt":
        assert moved.stat().st_mode & 0o777 == 0o600
    assert (store / f"{user.id}/10/key.p12").exists()


async def test_second_apply_finds_nothing(db, media, store):
    touch(media / "e/9_orphan.jpg")
    touch(store / "5/6/key.p12")
    os.utime(store / "5/6", (OLD, OLD))
    apply_reconcile(await plan_reconcile(db, media, store, NOW), media, store, NOW)
    again = await plan_reconcile(db, media, store, NOW)
    assert (again.orphan_files, again.stale_derived, again.returned_originals, again.orphan_packages) == ((), (), (), ())


async def test_without_pki_store_skips_packages(db, media):
    assert (await plan_reconcile(db, media, None, NOW)).orphan_packages == ()


async def test_cli_dry_run_then_apply(db, media, capsys):
    touch(media / "f/8_orphan.jpg")
    assert await run(db, apply=False, upload_path=media, store_dir=None, now=NOW) == 0
    out = capsys.readouterr().out
    assert "f/8_orphan.jpg" in out and "dry run" in out
    assert (media / "f/8_orphan.jpg").exists()
    assert (await db.exec(select(AuditLog).where(AuditLog.action == "reconcile"))).first() is None
    assert await run(db, apply=True, upload_path=media, store_dir=None, now=NOW) == 0
    assert not (media / "f/8_orphan.jpg").exists()
    row = (await db.exec(select(AuditLog).where(AuditLog.action == "reconcile"))).one()
    assert row.resource_type == "database" and row.event == "database.reconcile"
    assert row.params["orphan_files"] == 1 and row.details is None


async def test_fresh_derived_file_kept(db, media):
    """A poster the worker just made for an upload newer than the row snapshot (review)"""
    touch(media / "_derived/posters/000/7.jpg", mtime=NOW.timestamp() - 60)
    touch(media / "_derived/storyboards/000/8/sheet-001.jpg")
    os.utime(media / "_derived/storyboards/000/8", (NOW.timestamp() - 60, NOW.timestamp() - 60))
    assert (await plan_reconcile(db, media, None, NOW)).stale_derived == ()


async def test_files_are_listed_before_the_rows_are_read(db, media, monkeypatch):
    """A row is committed before its file lands: files listed first all have their row in the later snapshot"""
    from app.backend.services.backups import reconcile
    order = []
    real_files, real_rows = reconcile._media_files, reconcile._rows

    def files(root, now):
        order.append("files")
        return list(real_files(root, now))

    async def rows(session):
        order.append("rows")
        return await real_rows(session)

    monkeypatch.setattr(reconcile, "_media_files", files)
    monkeypatch.setattr(reconcile, "_rows", rows)
    await plan_reconcile(db, media, None, NOW)
    assert order == ["files", "rows"]


async def test_row_paths_in_other_forms_keep_their_files(db, media):
    """An absolute path left by migration 001, a path with backslashes, a row whose stored name matches (review)"""
    await media_row(db, 11, str(media / "2025/01/obs_1/11_a.jpg"))
    await media_row(db, 12, r"2025\01\obs_2\12_b.jpg")
    row = await media_row(db, 13, "somewhere/else/13_c.jpg")
    for rel in ("2025/01/obs_1/11_a.jpg", "2025/01/obs_2/12_b.jpg", f"2025/02/{row.stored_filename}"):
        touch(media / rel)
    report = await plan_reconcile(db, media, None, NOW)
    assert report.orphan_files == ()
    assert 11 not in report.missing_originals and 12 not in report.missing_originals
