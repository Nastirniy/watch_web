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
from app.backend.models.user_guide import UserGuide, UserGuideFile
from app.backend.services.backups.reconcile import MissingGuideFile, apply_reconcile, plan_reconcile
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


# ---------------------------------------------------------------- user guide files (_guides/<guide>/<file>/<stored>)

async def guide_file_row(db, guide_id: int, file_id: int, stored: str, file_name: str = "Manual.pdf",
                         is_deleted: bool = False) -> UserGuideFile:
    if await db.get(UserGuide, guide_id) is None:
        db.add(UserGuide(id=guide_id, title=f"Guide {guide_id}"))
    row = UserGuideFile(id=file_id, guide_id=guide_id, file_name=file_name, stored_name=stored,
                        file_path=f"_guides/{guide_id}/{file_id}/{stored}", content_type="application/pdf",
                        format_id=1, size_bytes=1, sha256="0" * 64, is_deleted=is_deleted)
    db.add(row)
    await db.commit()
    return row


def age(*folders: Path) -> None:
    """A folder is as old as its last change: set it after every file inside it is written"""
    for folder in folders:
        os.utime(folder, (OLD, OLD))


async def test_guide_file_with_its_row_stays(db, media):
    """A live and an archived file keep their files (archiving never deletes); the media walk skips _guides"""
    await guide_file_row(db, 1, 10, "aa.pdf")
    await guide_file_row(db, 1, 11, "bb.pdf", is_deleted=True)
    touch(media / "_guides/1/10/aa.pdf")
    touch(media / "_guides/1/11/bb.pdf")
    age(media / "_guides/1/10", media / "_guides/1/11")
    report = await plan_reconcile(db, media, None, NOW)
    assert report.orphan_guide_files == () and report.missing_guide_files == ()
    assert report.orphan_files == () and report.missing_originals == ()
    done = apply_reconcile(report, media, None, NOW)
    assert done.failed == ()
    assert (media / "_guides/1/10/aa.pdf").exists() and (media / "_guides/1/11/bb.pdf").exists()
    assert not (media / "_orphaned").exists()


async def test_guide_file_without_a_row_moves_with_its_path(db, media):
    """A stray file in a row's folder moves alone; a file's folder no row names moves whole"""
    await guide_file_row(db, 1, 10, "aa.pdf")
    touch(media / "_guides/1/10/aa.pdf")
    touch(media / "_guides/1/10/other.pdf", content=b"stray")      # the row's folder, not the row's stored name
    touch(media / "_guides/2/20/cc.pdf", content=b"after the backup")
    age(media / "_guides/1/10", media / "_guides/2/20")
    report = await plan_reconcile(db, media, None, NOW)
    assert report.orphan_guide_files == ("_guides/1/10/other.pdf", "_guides/2/20")
    assert report.orphan_files == ()
    done = apply_reconcile(report, media, None, NOW)
    assert done.orphan_guide_files == ("_guides/1/10/other.pdf", "_guides/2/20") and done.failed == ()
    assert (media / "_orphaned" / STAMP / "_guides/2/20/cc.pdf").read_bytes() == b"after the backup"
    assert (media / "_orphaned" / STAMP / "_guides/1/10/other.pdf").read_bytes() == b"stray"
    assert not (media / "_guides/2/20").exists() and not (media / "_guides/1/10/other.pdf").exists()
    assert (media / "_guides/1/10/aa.pdf").exists()


async def test_media_walk_does_not_treat_guides_as_media(db, media):
    touch(media / "_guides/4/40/dd.pdf")
    age(media / "_guides/4/40")
    touch(media / "2026/10/20/obs_1/1_aa.jpg")
    report = await plan_reconcile(db, media, None, NOW)
    assert report.orphan_files == ("2026/10/20/obs_1/1_aa.jpg",)
    assert report.orphan_guide_files == ("_guides/4/40",)


async def test_guide_row_without_its_file_is_listed(db, media):
    await guide_file_row(db, 1, 12, "gone.pdf", file_name="Handbook.pdf")
    await guide_file_row(db, 1, 13, "archived-gone.pdf", file_name="Old.pdf", is_deleted=True)
    report = await plan_reconcile(db, media, None, NOW)
    assert report.missing_guide_files == (MissingGuideFile(guide_id=1, file_id=12, file_name="Handbook.pdf"),
                                          MissingGuideFile(guide_id=1, file_id=13, file_name="Old.pdf"))
    assert report.missing_originals == () and report.returned_originals == ()
    assert apply_reconcile(report, media, None, NOW).missing_guide_files == report.missing_guide_files


async def test_guide_file_moved_back_from_orphaned(db, media):
    """An undo after an --apply: the moved guide file has its row again"""
    await guide_file_row(db, 1, 14, "back.pdf")
    touch(media / "_orphaned/20261022T100000Z/_guides/1/14/back.pdf", content=b"guide")
    report = await plan_reconcile(db, media, None, NOW)
    assert report.returned_originals == ("_orphaned/20261022T100000Z/_guides/1/14/back.pdf",)
    assert report.missing_guide_files == ()
    apply_reconcile(report, media, None, NOW)
    assert (media / "_guides/1/14/back.pdf").read_bytes() == b"guide"


async def test_young_guide_file_or_folder_left_alone(db, media):
    """A file renamed into place a moment ago (its row not committed yet): the folder is young even when the file
    (moved from the spool) is old"""
    touch(media / "_guides/5/50/young.pdf", mtime=NOW.timestamp() - 60)
    age(media / "_guides/5/50")
    touch(media / "_guides/5/51/placed.pdf")                       # old file, folder renamed into place just now
    os.utime(media / "_guides/5/51", (NOW.timestamp() - 5, NOW.timestamp() - 5))
    assert (await plan_reconcile(db, media, None, NOW)).orphan_guide_files == ()


async def test_guide_tmp_folder_young_stays_old_moves(db, media):
    """.tmp- folders of a guide upload: on their way while anything in them is younger than the age guard,
    interrupted (moved to _orphaned as a whole) once everything in them is older"""
    touch(media / "_guides/3/.tmp-30-aaaa1111/x.pdf", mtime=NOW.timestamp() - 60)      # being written
    touch(media / "_guides/3/.tmp-31-bbbb2222/y.pdf")                                  # interrupted
    touch(media / "_guides/3/.tmp-32-cccc3333/.tmp-0f0f-z.pdf", mtime=NOW.timestamp() - 30)   # a copy in progress
    age(media / "_guides/3/.tmp-31-bbbb2222", media / "_guides/3/.tmp-32-cccc3333")
    report = await plan_reconcile(db, media, None, NOW)
    assert report.orphan_guide_files == ("_guides/3/.tmp-31-bbbb2222",)
    assert report.orphan_files == ()
    done = apply_reconcile(report, media, None, NOW)
    assert done.orphan_guide_files == ("_guides/3/.tmp-31-bbbb2222",) and done.failed == ()
    assert (media / "_orphaned" / STAMP / "_guides/3/.tmp-31-bbbb2222/y.pdf").exists()
    assert not (media / "_guides/3/.tmp-31-bbbb2222").exists()
    assert (media / "_guides/3/.tmp-30-aaaa1111/x.pdf").exists()
    assert (media / "_guides/3/.tmp-32-cccc3333/.tmp-0f0f-z.pdf").exists()


async def test_guide_dry_run_changes_nothing(db, media):
    touch(media / "_guides/6/60/ee.pdf")
    touch(media / "_guides/6/.tmp-61-dddd4444/ff.pdf")
    age(media / "_guides/6/60", media / "_guides/6/.tmp-61-dddd4444")
    assert (await plan_reconcile(db, media, None, NOW)).orphan_guide_files == (
        "_guides/6/.tmp-61-dddd4444", "_guides/6/60")
    assert (media / "_guides/6/60/ee.pdf").exists() and (media / "_guides/6/.tmp-61-dddd4444/ff.pdf").exists()
    assert not (media / "_orphaned").exists()


async def test_cli_reports_guide_files(db, media, capsys):
    await guide_file_row(db, 7, 70, "gone.pdf", file_name="Handbook.pdf")
    touch(media / "_guides/7/71/stray.pdf")
    age(media / "_guides/7/71")
    assert await run(db, apply=False, upload_path=media, store_dir=None, now=NOW) == 0
    out = capsys.readouterr().out
    assert "1 user guide file or folder without a row" in out and "_guides/7/71" in out
    assert "1 user guide file row(s) whose file is missing" in out and "guide 7 file 70: Handbook.pdf" in out
    assert (media / "_guides/7/71/stray.pdf").exists()
    assert await run(db, apply=True, upload_path=media, store_dir=None, now=NOW) == 0
    assert (media / "_orphaned" / STAMP / "_guides/7/71/stray.pdf").exists()
    row = (await db.exec(select(AuditLog).where(AuditLog.action == "reconcile"))).one()
    # the event params are in the contract: guide files are counted with the media ones
    assert row.params["orphan_files"] == 1 and row.params["missing_originals"] == 1
    again = await plan_reconcile(db, media, None, NOW)
    assert again.orphan_guide_files == () and again.orphan_files == ()


async def test_a_file_folder_no_row_names_moves_whole_even_empty(db, media):
    """After a restore the guide tables may restart their ids at 1: a file's folder no row names (empty, or holding
    files) leaves _guides whole, so a new row of the same id never meets it; a row's folder stays"""
    await guide_file_row(db, 1, 10, "aa.pdf")
    touch(media / "_guides/1/10/aa.pdf")
    (media / "_guides/1/11").mkdir()                       # emptied by an older reconcile
    touch(media / "_guides/1/12/bb.pdf")
    age(media / "_guides/1/10", media / "_guides/1/11", media / "_guides/1/12", media / "_guides/1")
    report = await plan_reconcile(db, media, None, NOW)
    assert report.orphan_guide_files == ("_guides/1/11", "_guides/1/12")
    done = apply_reconcile(report, media, None, NOW)
    assert done.orphan_guide_files == ("_guides/1/11", "_guides/1/12") and done.failed == ()
    assert (media / "_orphaned" / STAMP / "_guides/1/11").is_dir()
    assert (media / "_orphaned" / STAMP / "_guides/1/12/bb.pdf").exists()
    assert sorted(p.name for p in (media / "_guides/1").iterdir()) == ["10"]


async def test_a_guide_folder_no_row_names_moves_whole(db, media):
    """No row of the guide: its folder goes as one entry, with its file folders and an interrupted .tmp- folder"""
    touch(media / "_guides/3/30/a.pdf")
    (media / "_guides/3/31").mkdir()
    touch(media / "_guides/3/.tmp-32-eeee5555/y.pdf")
    age(media / "_guides/3/30", media / "_guides/3/31", media / "_guides/3/.tmp-32-eeee5555", media / "_guides/3")
    report = await plan_reconcile(db, media, None, NOW)
    assert report.orphan_guide_files == ("_guides/3",)
    done = apply_reconcile(report, media, None, NOW)
    assert done.orphan_guide_files == ("_guides/3",) and done.failed == ()
    assert not (media / "_guides/3").exists()
    moved = media / "_orphaned" / STAMP / "_guides/3"
    assert (moved / "30/a.pdf").exists() and (moved / "31").is_dir() and (moved / ".tmp-32-eeee5555/y.pdf").exists()


async def test_a_young_guide_folder_moves_by_its_parts(db, media):
    """A guide folder changed a moment ago (an upload placed a folder in it) does not move whole: only its old
    parts no row names do"""
    touch(media / "_guides/2/20/a.pdf")
    touch(media / "_guides/2/21/b.pdf", mtime=NOW.timestamp() - 5)
    age(media / "_guides/2/20")
    os.utime(media / "_guides/2/21", (NOW.timestamp() - 5, NOW.timestamp() - 5))
    os.utime(media / "_guides/2", (NOW.timestamp() - 5, NOW.timestamp() - 5))
    report = await plan_reconcile(db, media, None, NOW)
    assert report.orphan_guide_files == ("_guides/2/20",)


async def test_apply_checks_every_guide_entry_again(db, media):
    """Between the plan and the apply a guide file, a file folder and a .tmp- folder become young (an upload on its
    way, a file written): apply leaves each where it is, and nothing fails"""
    await guide_file_row(db, 1, 10, "aa.pdf")
    touch(media / "_guides/1/10/aa.pdf")
    touch(media / "_guides/1/10/stray.pdf")
    touch(media / "_guides/1/11/b.pdf")
    touch(media / "_guides/1/.tmp-12-ffff6666/c.pdf")
    age(media / "_guides/1/10", media / "_guides/1/11", media / "_guides/1/.tmp-12-ffff6666")
    report = await plan_reconcile(db, media, None, NOW)
    assert report.orphan_guide_files == ("_guides/1/.tmp-12-ffff6666", "_guides/1/10/stray.pdf", "_guides/1/11")
    young = NOW.timestamp() - 30
    os.utime(media / "_guides/1/10/stray.pdf", (young, young))
    touch(media / "_guides/1/11/new.pdf", mtime=young)
    touch(media / "_guides/1/.tmp-12-ffff6666/.tmp-0a0a-d.pdf", mtime=young)
    done = apply_reconcile(report, media, None, NOW)
    assert done.orphan_guide_files == () and done.failed == ()
    assert (media / "_guides/1/10/stray.pdf").exists() and (media / "_guides/1/11/b.pdf").exists()
    assert (media / "_guides/1/.tmp-12-ffff6666/c.pdf").exists()
    assert not (media / "_orphaned").exists()


async def test_hidden_and_nas_names_under_guides_are_left_alone(db, media):
    """As in the media walk: hidden files, NAS index / recycle folders and lost+found under _guides are never
    listed nor moved, and a folder holding one never moves whole (its own old files move one by one)"""
    for rel in ("_guides/@eaDir/x.jpg", "_guides/.DS_Store", "_guides/#recycle/old.pdf", "_guides/lost+found/f",
                "_guides/8/@eaDir/thumb.jpg", "_guides/8/.x", "_guides/8/80/.hidden", "_guides/8/80/stray.pdf",
                "_guides/9/90/@eaDir/a/b.jpg", "_guides/9/90/c.pdf"):
        touch(media / rel)
    age(media / "_guides/@eaDir", media / "_guides/#recycle", media / "_guides/lost+found",
        media / "_guides/8/@eaDir", media / "_guides/8/80", media / "_guides/8",
        media / "_guides/9/90/@eaDir/a", media / "_guides/9/90/@eaDir", media / "_guides/9/90", media / "_guides/9",
        media / "_guides")
    report = await plan_reconcile(db, media, None, NOW)
    assert report.orphan_guide_files == ("_guides/8/80/stray.pdf", "_guides/9/90/c.pdf")
    assert report.orphan_files == ()
    done = apply_reconcile(report, media, None, NOW)
    assert done.orphan_guide_files == ("_guides/8/80/stray.pdf", "_guides/9/90/c.pdf") and done.failed == ()
    for rel in ("_guides/@eaDir/x.jpg", "_guides/.DS_Store", "_guides/#recycle/old.pdf", "_guides/lost+found/f",
                "_guides/8/@eaDir/thumb.jpg", "_guides/8/.x", "_guides/8/80/.hidden", "_guides/9/90/@eaDir/a/b.jpg"):
        assert (media / rel).exists(), rel


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
async def test_folders_made_for_a_returned_guide_file_are_0750(db, media):
    """A guide file moved back from _orphaned gets the folders it needs as the store makes them, whatever the
    umask"""
    await guide_file_row(db, 1, 14, "back.pdf")
    touch(media / "_orphaned/20261022T100000Z/_guides/1/14/back.pdf", content=b"guide")
    report = await plan_reconcile(db, media, None, NOW)
    previous = os.umask(0o077)
    try:
        done = apply_reconcile(report, media, None, NOW)
    finally:
        os.umask(previous)
    assert done.returned_originals == ("_orphaned/20261022T100000Z/_guides/1/14/back.pdf",) and done.failed == ()
    for folder in ("_guides", "_guides/1", "_guides/1/14"):
        assert (media / folder).stat().st_mode & 0o777 == 0o750, folder
