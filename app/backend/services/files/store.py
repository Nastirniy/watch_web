"""Files into storage: the spool of a multipart body, the move into storage, and the files of user guides.

spool_upload copies an uploaded body to the spool directory in 1 MB chunks, never whole into memory (the media upload
and the guide upload use it: one implementation). move_into_place moves a file into storage: a rename, or across
file systems a copy to a temporary name replaced into place.

A guide file lives at <upload_path>/_guides/<guide_id>/<file_id>/<stored_name> (stored_name: uuid hex plus the
extension). vet_guide_file holds the spooled file to its document format (the extension, the size against
max_file_size_bytes capped by storage.max_file_size_gb, the sniffed type against the format's content type and
aliases, the text rule of a text/* format) and hashes it, before the caller locks the guide; store_guide_file then
inserts the row and writes its folder through a .tmp- directory renamed into place (files 0640, folders 0750). The row is flushed but not committed: the caller adds its audit row and
commits, and calls discard_guide_file when that fails, so a file on disk always has its row. On any failure here
the folder written so far is removed (with the guide's folder when that leaves it empty) and the transaction rolled
back. The files sit in the media directory, so
nginx serves them from /protected_uploads/ like the media originals.

No FastAPI: errors are CodedError (UnsupportedMediaFormat 415, FileTooLarge 413 through the route); the route
converts them. Blocking file work runs in a thread.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiofiles
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.config import settings
from app.backend.enums.errors import ErrorCode
from app.backend.enums.general import MediaType, UploadPurpose
from app.backend.errors.coded import CodedError
from app.backend.models.user_guide import UserGuide, UserGuideFile
from app.backend.services.files.formats import FileTooLarge, FormatSpec, UnsupportedMediaFormat, load_registry
from app.backend.services.files.names import DANGEROUS_EXTENSIONS, extension_of, sanitize_filename
from app.backend.services.files.sniff import check_text, detect_content_type, is_text_format
from app.backend.utils.logging_config import get_logger

_logger = get_logger("file_store")

CHUNK_BYTES = 1024 * 1024
GUIDES_DIR = "_guides"
GUIDE_FILE_MODE = 0o640
GUIDE_DIR_MODE = 0o750
TEMP_PREFIX = ".tmp-"
# Where an entry in the way of the store is moved (restore_reconcile moves what no row names there too)
ORPHANED_DIR = "_orphaned"


# ------------------------------------------------------------------ spool and move

async def spool_upload(upload, temp_dir: Path, chunk_size: int = CHUNK_BYTES) -> Path:
    """Copy an uploaded body (Starlette UploadFile) to a new file of temp_dir in chunks; the caller removes it once
    the file is stored or refused. A copy that fails halfway is removed and the error raised."""
    temp_dir.mkdir(parents=True, exist_ok=True)
    suffix = extension_of(sanitize_filename(upload.filename or ""))[:10]
    target = temp_dir / f"upload_{uuid.uuid4().hex}{suffix}"
    try:
        async with aiofiles.open(target, "wb") as out:
            while chunk := await upload.read(chunk_size):
                await out.write(chunk)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


def move_into_place(source: Path, destination: Path, mode: int = 0o644) -> None:
    """Rename on the same filesystem, copy and delete across filesystems (blocking: run it in a thread).

    The copy goes to a temporary name in the destination directory and is replaced into place, so a copy that fails
    halfway (a full share, a dropped mount) never leaves a partial file under the final name. On any failure the
    temporary file is removed and the error raised; the source stays for the caller's cleanup."""
    try:
        source.rename(destination)
    except OSError:
        temporary = destination.with_name(f"{TEMP_PREFIX}{uuid.uuid4().hex}-{destination.name}")
        try:
            shutil.copy2(str(source), str(temporary))
            os.replace(temporary, destination)
        except BaseException:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as cleanup_error:
                _logger.warning(f"Could not remove temporary copy {temporary}: {cleanup_error!r}")
            raise
        source.unlink()
    os.chmod(destination, mode)


def upload_root() -> Path:
    """The uploads root (storage.upload_path), absolute"""
    root = Path(settings.storage.upload_path)
    return root if root.is_absolute() else root.resolve()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        while chunk := source.read(CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


# ------------------------------------------------------------------ guide files

@dataclass(frozen=True)
class VettedGuideFile:
    """A spooled file that passed the checks of a guide file (vet_guide_file), with its sha256"""
    path: Path              # the spooled file the checks and the hash read: the one store_guide_file moves
    spec: FormatSpec
    file_name: str          # sanitised original name (names.sanitize_filename)
    extension: str
    size_bytes: int
    sha256: str


def guide_file_path(guide_id: int, file_id: int, stored_name: str) -> str:
    """The file's path relative to the uploads root (user_guide_files.file_path)"""
    return f"{GUIDES_DIR}/{guide_id}/{file_id}/{stored_name}"


def guide_file_location(row: UserGuideFile) -> Path:
    """Where the file of the row is on disk"""
    return upload_root() / row.file_path


async def vet_guide_file(session: AsyncSession, source: Path, original_name: str) -> VettedGuideFile:
    """The checks of a guide file, in order: the extension (dangerous, then a document format), the size, the
    sniffed type against the format's content type and aliases, the text rule; then the sha256. Reads the file in
    chunks, in threads. The slow part of an upload: the caller runs it before its write takes the guide's lock, on
    the spooled file it then passes to store_guide_file (a file of the spool directory under a random name: nothing
    else writes it). A refused file raises UnsupportedMediaFormat, FileTooLarge or CodedError media_file_empty."""
    document = MediaType.DOCUMENT.value
    file_name = sanitize_filename(original_name)
    extension = extension_of(file_name)
    if extension in DANGEROUS_EXTENSIONS:
        raise UnsupportedMediaFormat(ErrorCode.MEDIA_EXTENSION_DANGEROUS, document, extension=extension)
    spec = (await load_registry(session)).resolve(UploadPurpose.USER_GUIDE, file_name)
    if spec is None:
        raise UnsupportedMediaFormat(ErrorCode.MEDIA_EXTENSION_NOT_ACCEPTED, document, extension=extension)

    size = source.stat().st_size
    if size > spec.effective_limit_bytes:
        raise FileTooLarge(f"File size {size} exceeds the limit of {spec.effective_limit_bytes} bytes",
                           spec.effective_limit_bytes, size)
    if size == 0:
        raise CodedError(ErrorCode.MEDIA_FILE_EMPTY)

    sniffed = await asyncio.to_thread(detect_content_type, source)
    if sniffed not in {mime.lower() for mime in spec.all_mimes}:
        raise UnsupportedMediaFormat(ErrorCode.MEDIA_CONTENT_TYPE_NOT_ACCEPTED, document, content_type=sniffed)
    if is_text_format(spec.content_type):
        await asyncio.to_thread(check_text, source, spec.content_type)
    return VettedGuideFile(path=source, spec=spec, file_name=file_name, extension=extension, size_bytes=size,
                           sha256=await asyncio.to_thread(_sha256, source))


def _make_dir(path: Path) -> None:
    """A folder of the guide tree, 0750 when this call creates it (another upload may create it at the same time)"""
    try:
        path.mkdir(mode=GUIDE_DIR_MODE)
    except FileExistsError:
        return
    os.chmod(path, GUIDE_DIR_MODE)   # the umask may have narrowed the mode given to mkdir()


def _place(temporary: Path, target: Path) -> None:
    """The .tmp- folder becomes the file's folder (one rename on the same filesystem)"""
    os.rename(temporary, target)


def _set_aside(root: Path, path: Path) -> None:
    """Move an entry the store finds where its own folder belongs to <root>/_orphaned/<time>/<same path>, as it is,
    never deleted (as restore_reconcile moves what no row names). Raises FileExistsError when it cannot, so the
    caller's cleanup leaves the entry alone."""
    relative = path.relative_to(root)
    stamp = f"{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    destination = root / ORPHANED_DIR / stamp / relative
    if destination.exists() or destination.is_symlink():
        destination = root / ORPHANED_DIR / f"{stamp}-{secrets.token_hex(4)}" / relative
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(path, destination)
    except FileNotFoundError:
        if path.exists() or path.is_symlink():      # the destination's side failed, not a source already gone
            raise FileExistsError(f"{relative.as_posix()} not moved aside")
        return
    except OSError as exc:
        raise FileExistsError(f"{relative.as_posix()} not moved aside") from exc
    _logger.warning(f"User guide store: {relative.as_posix()} was in the way, moved to "
                    f"{destination.relative_to(root).as_posix()}")


def _clear_place(root: Path, target: Path) -> None:
    """The file's folder place free: an empty folder is removed (reused), anything else (a folder holding files or
    a NAS index folder such as @eaDir, a file, a symlink) moved aside. No row has the new file's id, so nothing
    there belongs to a live file: it is a leftover (after a restore the id counter of a table the backup lacks
    starts again at 1, and restore_reconcile leaves folders holding hidden or NAS names in place)."""
    if not (target.exists() or target.is_symlink()):
        return
    if target.is_dir() and not target.is_symlink():
        try:
            target.rmdir()          # only an empty folder goes
            return
        except OSError:
            pass
    _set_aside(root, target)


def _write_guide_folder(root: Path, guide_id: int, file_id: int, stored_name: str, source: Path) -> None:
    """<root>/_guides/<guide_id>/<file_id>/<stored_name> through a .tmp- folder renamed into place (blocking).

    Whatever is already at the file's place is not this file's (_clear_place): an empty folder is reused, anything
    else moved to _orphaned/<time>/; so is a file or a symlink where the guide's folder belongs. An entry that cannot
    be moved is FileExistsError and left as it is. On any failure the .tmp- folder is removed and the error raised.
    The source is moved, not copied, when it is on the same filesystem."""
    root.mkdir(parents=True, exist_ok=True)
    guides = root / GUIDES_DIR
    guide_dir = guides / str(guide_id)
    target = guide_dir / str(file_id)
    temporary = guide_dir / f"{TEMP_PREFIX}{file_id}-{secrets.token_hex(4)}"
    for attempt in range(3):
        _make_dir(guides)
        if guide_dir.is_symlink() or (guide_dir.exists() and not guide_dir.is_dir()):
            _set_aside(root, guide_dir)
        _make_dir(guide_dir)
        _clear_place(root, target)
        try:
            temporary.mkdir(mode=GUIDE_DIR_MODE)
            break
        except FileNotFoundError:
            # the guide's folder, empty, removed meanwhile by the cleanup of another upload that failed
            # (_remove_guide_folder): made again
            if attempt == 2:
                raise
    try:
        os.chmod(temporary, GUIDE_DIR_MODE)
        move_into_place(source, temporary / stored_name, GUIDE_FILE_MODE)
        _place(temporary, target)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _remove_guide_folder(root: Path, guide_id: int, file_id: int) -> None:
    """The file's folder, and the guide's folder when that leaves it empty (a failed first upload of the guide)"""
    guide_dir = root / GUIDES_DIR / str(guide_id)
    shutil.rmtree(guide_dir / str(file_id), ignore_errors=True)
    try:
        guide_dir.rmdir()           # only an empty folder goes: another file, or an upload's .tmp- folder, keeps it
    except OSError:
        pass


async def store_guide_file(session: AsyncSession, guide: UserGuide, vetted: VettedGuideFile,
                           language: Optional[str], user_id: int) -> UserGuideFile:
    """Store a vetted spooled file (vet_guide_file) as a file of the guide; the guide row is locked by the caller
    (lock_first), so this is part of its write.

    Returns the row, flushed (its id set) with its file in place, NOT committed: the caller adds its audit row and
    commits, and calls discard_guide_file(session, row) when either fails. A filesystem error while writing is
    CodedError media_store_failed (logged); nothing written stays and the transaction is rolled back. The row
    stores the format's canonical content type, never the sniffed alias."""
    guide_id = guide.id      # read before any rollback below expires the row
    spooled = vetted.path
    stored_name = f"{uuid.uuid4().hex}{vetted.extension}"
    row = UserGuideFile(
        guide_id=guide_id, file_name=vetted.file_name, stored_name=stored_name, file_path="",
        content_type=vetted.spec.content_type, format_id=vetted.spec.id, size_bytes=vetted.size_bytes,
        sha256=vetted.sha256, language=language, created_at=datetime.now(timezone.utc), created_by=user_id,
    )
    session.add(row)
    await session.flush()       # the id names the folder
    file_id = row.id
    row.file_path = guide_file_path(guide_id, file_id, stored_name)
    root = upload_root()
    try:
        await asyncio.to_thread(_write_guide_folder, root, guide_id, file_id, stored_name, spooled)
        await session.flush()
    except BaseException as exc:
        # The folder of this row only: a FileExistsError means one that was there before, which is not ours
        if not isinstance(exc, FileExistsError):
            await asyncio.to_thread(_remove_guide_folder, root, guide_id, file_id)
        await session.rollback()
        if isinstance(exc, OSError):
            _logger.error(f"Storing user guide file {file_id} of guide {guide_id} failed: {exc!r}")
            raise CodedError(ErrorCode.MEDIA_STORE_FAILED) from exc
        raise
    return row


async def discard_guide_file(session: AsyncSession, row: UserGuideFile) -> None:
    """Undo store_guide_file when the caller's audit row or commit failed: the file's folder removed, then the
    transaction rolled back (as store_guide_file does: a rollback that fails, a lost connection, must not leave the
    folder without its row). The caller re-raises its error."""
    guide_id, file_id = row.guide_id, row.id     # read before the rollback expires the row
    try:
        await asyncio.to_thread(_remove_guide_folder, upload_root(), guide_id, file_id)
    finally:
        await session.rollback()
