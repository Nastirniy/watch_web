"""Resumable upload sessions: create, append chunks, complete, abort, expire.

Progress lives in upload_sessions.received_bytes; the bytes live in one part
file per session under <upload_path>/_tmp/uploads. Only one PATCH may write
to a session at a time (writer_token, claimed with a conditional UPDATE), and
a chunk is accepted only when its Upload-Offset equals received_bytes, so a
retried or duplicated request can never corrupt the file: the worst case is a
409 telling the client the real offset.

Completion hands the part file to MediaManager.store_file_from_path, the same
path the nginx-offloaded upload takes, so every format, size and hash rule
applies once, in one place.
"""
from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AsyncIterator, Dict, Iterable, List, Optional

import aiofiles
import aiofiles.os
from sqlalchemy import update
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.config import settings
from app.backend.enums.errors import ErrorCode
from app.backend.models.upload_session import UploadSession
from app.backend.errors.coded import CodedError

PART_DIR = "_tmp/uploads"
WRITER_STALE_SECONDS = 180

STATUS_OPEN = "open"
STATUS_COMPLETED = "completed"
STATUS_ABORTED = "aborted"
STATUS_EXPIRED = "expired"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


class OffsetMismatch(Exception):
    def __init__(self, current_offset: int):
        super().__init__(f"offset mismatch, current offset is {current_offset}")
        self.current_offset = current_offset


class WriterBusy(Exception):
    """Another chunk is being written to this session right now"""


class ChunkTooLarge(CodedError):
    """The streamed chunk went past max_chunk_bytes or past the declared file size"""


def part_path_for(upload_id: str) -> str:
    return f"{PART_DIR}/{upload_id}.part"


def resolve_part(session_row: UploadSession, storage_root: Path) -> Path:
    return storage_root / session_row.part_path


def new_session(
    user_id: int, observation_id: int, post_id: int, media_type: str, original_filename: str,
    expected_size: int, declared_content_type: Optional[str], expected_sha256: Optional[str],
) -> UploadSession:
    upload_id = uuid.uuid4().hex
    now = utcnow()
    return UploadSession(
        id=upload_id, user_id=user_id, observation_id=observation_id, post_id=post_id,
        media_type=media_type, original_filename=original_filename,
        declared_content_type=declared_content_type, expected_size=int(expected_size),
        expected_sha256=expected_sha256.lower() if expected_sha256 else None,
        part_path=part_path_for(upload_id),
        expires_at=now + timedelta(hours=settings.storage.upload.session_ttl_hours),
    )


async def get_session_row(session: AsyncSession, upload_id: str) -> Optional[UploadSession]:
    return (await session.exec(
        select(UploadSession).where(UploadSession.id == upload_id).execution_options(populate_existing=True)
    )).first()


async def list_sessions(
    session: AsyncSession,
    user_id: Optional[int],
    status: Optional[str],
    observation_id: Optional[int],
    post_ids: Optional[Iterable[int]] = None,
) -> List[UploadSession]:
    """user_id limits to one uploader, post_ids to sessions opened for those posts (None: no limit)"""
    query = select(UploadSession)
    if user_id is not None:
        query = query.where(UploadSession.user_id == user_id)
    if post_ids is not None:
        query = query.where(UploadSession.post_id.in_(list(post_ids)))
    if status:
        query = query.where(UploadSession.status == status)
    if observation_id is not None:
        query = query.where(UploadSession.observation_id == observation_id)
    return list((await session.exec(query.order_by(UploadSession.created_at.desc()))).all())


# ---- chunk writing -----------------------------------------------------------

async def claim_writer(session: AsyncSession, upload_id: str, offset: int) -> str:
    """Take the writer token for one chunk. Raises OffsetMismatch or WriterBusy."""
    now = utcnow()
    token = uuid.uuid4().hex
    stale = now - timedelta(seconds=WRITER_STALE_SECONDS)
    result = await session.execute(
        update(UploadSession)
        .execution_options(synchronize_session=False)
        .where(
            UploadSession.id == upload_id,
            UploadSession.status == STATUS_OPEN,
            UploadSession.received_bytes == offset,
            (UploadSession.writer_token.is_(None)) | (UploadSession.writer_since < stale),
        )
        .values(writer_token=token, writer_since=now, last_activity_at=now)
    )
    await session.commit()
    if result.rowcount == 1:
        return token
    row = await get_session_row(session, upload_id)
    if row is None or row.status != STATUS_OPEN:
        raise LookupError("session is not open")
    if int(row.received_bytes) != offset:
        raise OffsetMismatch(int(row.received_bytes))
    raise WriterBusy()


async def append_chunk(
    session: AsyncSession, row: UploadSession, token: str, storage_root: Path,
    body: AsyncIterator[bytes], offset: int, max_chunk_bytes: int,
) -> int:
    """Stream the request body onto the part file at offset. Returns the new offset.

    On any failure the file is truncated back to offset and the token released,
    so the session stays consistent with received_bytes.
    """
    part = resolve_part(row, storage_root)
    part.parent.mkdir(parents=True, exist_ok=True)
    remaining = int(row.expected_size) - offset
    written = 0
    try:
        async with aiofiles.open(part, "ab") as handle:
            # Defensive: the file must be exactly `offset` long before appending
            await handle.seek(0, 2)
            current = await handle.tell()
            if current != offset:
                await handle.truncate(offset)
                await handle.seek(offset)
            async for piece in body:
                if not piece:
                    continue
                written += len(piece)
                if written > max_chunk_bytes:
                    raise ChunkTooLarge(ErrorCode.UPLOAD_CHUNK_TOO_LARGE, max_chunk_bytes=max_chunk_bytes)
                if written > remaining:
                    raise ChunkTooLarge(ErrorCode.UPLOAD_CHUNK_OVERRUN, expected_size=int(row.expected_size))
                await handle.write(piece)
            await handle.flush()
    except BaseException:
        try:
            async with aiofiles.open(part, "r+b") as handle:
                await handle.truncate(offset)
        except FileNotFoundError:
            pass
        await release_writer(session, row.id, token, offset)
        raise

    new_offset = offset + written
    await release_writer(session, row.id, token, new_offset)
    return new_offset


async def release_writer(session: AsyncSession, upload_id: str, token: str, received_bytes: int) -> None:
    now = utcnow()
    await session.execute(
        update(UploadSession)
        .execution_options(synchronize_session=False)
        .where(UploadSession.id == upload_id, UploadSession.writer_token == token)
        .values(writer_token=None, writer_since=None, received_bytes=received_bytes, last_activity_at=now, updated_at=now)
    )
    await session.commit()


# ---- completion, abort, expiry -----------------------------------------------

async def sha256_of(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    async with aiofiles.open(path, "rb") as handle:
        while True:
            piece = await handle.read(chunk_size)
            if not piece:
                break
            digest.update(piece)
    return digest.hexdigest()


async def mark(session: AsyncSession, row: UploadSession, status: str, error: Optional[str] = None, media_file_id: Optional[int] = None) -> None:
    now = utcnow()
    row.status = status
    row.error = error[:500] if error else None
    row.updated_at = now
    row.last_activity_at = now
    if media_file_id is not None:
        row.media_file_id = media_file_id
    if status == STATUS_COMPLETED:
        row.completed_at = now
    session.add(row)
    await session.commit()


async def remove_part(row: UploadSession, storage_root: Path) -> None:
    part = resolve_part(row, storage_root)
    try:
        await aiofiles.os.remove(part)
    except FileNotFoundError:
        pass


async def expire_sessions(session: AsyncSession, storage_root: Path, now: Optional[datetime] = None) -> Dict[str, int]:
    """Open sessions past expires_at become expired and lose their part file; finished sessions older than
    twice the ttl lose a leftover part file too (rows are kept for the audit trail)."""
    now = now or utcnow()
    counts = {"expired": 0, "cleaned": 0}
    rows = (await session.exec(select(UploadSession).where(UploadSession.status == STATUS_OPEN))).all()
    for row in rows:
        if _aware(row.expires_at) <= now:
            await remove_part(row, storage_root)
            await mark(session, row, STATUS_EXPIRED, error="session expired before completion")
            counts["expired"] += 1
    cutoff = now - timedelta(hours=2 * settings.storage.upload.session_ttl_hours)
    finished = (await session.exec(
        select(UploadSession).where(UploadSession.status != STATUS_OPEN, UploadSession.updated_at < cutoff)
    )).all()
    for row in finished:
        part = resolve_part(row, storage_root)
        if part.exists():
            await remove_part(row, storage_root)
            counts["cleaned"] += 1
    return counts


def is_expired(row: UploadSession, now: Optional[datetime] = None) -> bool:
    return _aware(row.expires_at) <= (now or utcnow())


# ---------------------------------------------------------------------------
# Files nginx wrote for offloaded uploads (client_body_in_file_only)
# ---------------------------------------------------------------------------

def resolve_nginx_temp_file(name: Optional[str], directory: str) -> Optional[Path]:
    """The regular file `name` (nginx's X-File-Name) points at, only when it lies inside `directory`.

    Both are resolved first, so `..` and symbolic links cannot lead out; nginx's own sub-directories
    (client_body_temp_path levels) are inside. Anything else is None and the file is left alone.
    """
    if not name or "\x00" in name:
        return None
    try:
        root = Path(directory).resolve()
        candidate = Path(name).resolve()
        if candidate == root or not candidate.is_relative_to(root) or not candidate.is_file():
            return None
    except (OSError, ValueError):
        return None
    return candidate


@dataclass(frozen=True)
class SweepResult:
    files: int
    bytes: int


def sweep_nginx_temp(directory: Path, max_age_seconds: float, now: Optional[float] = None) -> SweepResult:
    """Delete files under `directory` not modified for `max_age_seconds`; directories stay (nginx owns them)"""
    root = Path(directory)
    if not root.is_dir():
        return SweepResult(0, 0)
    cutoff = (time.time() if now is None else now) - max_age_seconds
    files = freed = 0
    for path in root.rglob("*"):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            info = path.stat()
            if info.st_mtime >= cutoff:
                continue
            path.unlink()
        except FileNotFoundError:
            continue  # nginx or the API removed it meanwhile
        files += 1
        freed += info.st_size
    return SweepResult(files, freed)
