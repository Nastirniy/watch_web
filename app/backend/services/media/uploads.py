"""Resumable upload sessions: create, append chunks, complete, abort, expire.

Progress lives in upload_sessions.received_bytes; the bytes live in one part
file per session under <upload_path>/_tmp/uploads. One request works on a
session at a time and holds writer_token while it does (taken with a
conditional UPDATE): a PATCH while it streams a chunk, or a complete while it
stores the file (status `completing`). A chunk is accepted only when its
Upload-Offset equals received_bytes, so a retried or duplicated request can
never corrupt the file: the worst case is a 409 telling the client the real
offset.

A claim left by a request that died goes stale (WRITER_STALE_SECONDS for a
chunk, COMPLETION_STALE_SECONDS for a completion) and the next request takes
it over. A chunk writer renews its claim while it streams and checks it still
holds it before every write, before truncating and before releasing: a writer
that lost the session leaves the part file and the row alone.

Completion hands the part file to MediaManager.store_file_from_path, the same
path the nginx-offloaded upload takes, so every format, size and hash rule
applies once, in one place.

`error` of a session is the problem code that ended it, never an internal
reason (those go to the log).
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
from sqlalchemy import or_, update
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.config import settings
from app.backend.enums.errors import ErrorCode
from app.backend.models.media import MediaFile
from app.backend.models.upload_session import UploadSession
from app.backend.errors.coded import CodedError
from app.backend.utils.logging_config import get_logger

_logger = get_logger("uploads")

PART_DIR = "_tmp/uploads"
# A chunk writer that has not renewed its claim for this long is gone: the next PATCH takes the session over
WRITER_STALE_SECONDS = 180
# How often a streaming writer renews its claim (and checks it still holds it); well below WRITER_STALE_SECONDS
WRITER_RENEW_SECONDS = 30
# A completion claim this old was left by a request that died. Completion reads files of up to
# storage.max_file_size_gb twice (sha256, then the manager's hash) and is not renewed while the manager stores
# the file, so the bound is generous
COMPLETION_STALE_SECONDS = 3600

STATUS_OPEN = "open"
STATUS_COMPLETING = "completing"
STATUS_COMPLETED = "completed"
STATUS_ABORTED = "aborted"
STATUS_EXPIRED = "expired"

# What `error` may answer: problem codes (rows written before codes hold free text, never answered)
_PROBLEM_CODES = frozenset(code.value for code in ErrorCode)


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


class WriterLost(Exception):
    """This writer's claim went stale and another request took the session, or the session ended: the writer
    stops and touches neither the part file nor the row"""


class ChunkTooLarge(CodedError):
    """The streamed chunk went past max_chunk_bytes or past the declared file size"""


class BytesMissing(CodedError):
    """The part file holds fewer bytes than received_bytes: the session was aborted"""


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
    user_id: int,
    status: Optional[str],
    observation_id: Optional[int],
    others_post_ids: Optional[Iterable[int]] = frozenset(),
) -> List[UploadSession]:
    """The sessions of user_id, plus other users' sessions opened for others_post_ids (None: every post; empty:
    none)"""
    query = select(UploadSession)
    if others_post_ids is not None:
        own = UploadSession.user_id == user_id
        others = sorted(others_post_ids)
        query = query.where(or_(own, UploadSession.post_id.in_(others)) if others else own)
    if status:
        query = query.where(UploadSession.status == status)
    if observation_id is not None:
        query = query.where(UploadSession.observation_id == observation_id)
    return list((await session.exec(query.order_by(UploadSession.created_at.desc()))).all())


# ---- claims ------------------------------------------------------------------

def _writer_free(now: datetime):
    """No chunk writer, or one that stopped renewing its claim"""
    return UploadSession.writer_token.is_(None) | (UploadSession.writer_since < now - timedelta(seconds=WRITER_STALE_SECONDS))


def _completion_stale(now: datetime):
    return (UploadSession.status == STATUS_COMPLETING) & (
        UploadSession.writer_since < now - timedelta(seconds=COMPLETION_STALE_SECONDS))


def _endable(now: datetime):
    """Open (a chunk writer may be streaming: it stops at its next check), or a completion claim that went stale.
    A completion that is running is left to finish."""
    return (UploadSession.status == STATUS_OPEN) | _completion_stale(now)


def _holds_completion(token: str):
    return (UploadSession.status == STATUS_COMPLETING) & (UploadSession.writer_token == token)


async def _update(session: AsyncSession, upload_id: str, now: datetime, *conditions, **values) -> bool:
    """One conditional UPDATE of the session row, committed; True when the row matched"""
    result = await session.execute(
        update(UploadSession)
        .execution_options(synchronize_session=False)
        .where(UploadSession.id == upload_id, *conditions)
        .values(updated_at=now, last_activity_at=now, **values)
    )
    await session.commit()
    return result.rowcount == 1


# ---- chunk writing -----------------------------------------------------------

async def claim_writer(session: AsyncSession, upload_id: str, offset: int) -> str:
    """Take the writer token for one chunk. Raises OffsetMismatch or WriterBusy (LookupError: not open)."""
    now = utcnow()
    token = uuid.uuid4().hex
    claimed = await _update(
        session, upload_id, now,
        UploadSession.status == STATUS_OPEN, UploadSession.received_bytes == offset, _writer_free(now),
        writer_token=token, writer_since=now,
    )
    if claimed:
        return token
    row = await get_session_row(session, upload_id)
    if row is None or row.status != STATUS_OPEN:
        raise LookupError("session is not open")
    if int(row.received_bytes) != offset:
        raise OffsetMismatch(int(row.received_bytes))
    raise WriterBusy()


async def renew_writer(session: AsyncSession, upload_id: str, token: str) -> bool:
    """Extend this writer's claim by WRITER_STALE_SECONDS; False when it no longer holds an open session"""
    now = utcnow()
    return await _update(session, upload_id, now, UploadSession.status == STATUS_OPEN, UploadSession.writer_token == token,
                         writer_since=now)


async def append_chunk(
    session: AsyncSession, row: UploadSession, token: str, storage_root: Path,
    body: AsyncIterator[bytes], offset: int, max_chunk_bytes: int,
) -> int:
    """Stream the request body onto the part file at offset. Returns the new offset.

    The part file must hold exactly `offset` bytes first: the leftover of a chunk that broke off is cut away, a
    file that is shorter aborts the session (BytesMissing; a gap is never filled). While streaming, the claim is
    renewed every WRITER_RENEW_SECONDS, checked before the next write; a writer that lost it raises WriterLost and
    leaves the file and the row alone. On any other failure the session is rolled back (the failure may have come
    from it; this expires its ORM objects, `row` included: callers read the row again), then the file is truncated
    back to offset and the token released, when this writer still holds it, so the session stays consistent with
    received_bytes.
    """
    upload_id = row.id
    expected_size = int(row.expected_size)
    part = resolve_part(row, storage_root)
    part.parent.mkdir(parents=True, exist_ok=True)
    try:
        size = part.stat().st_size
    except FileNotFoundError:
        size = 0
    if size < offset:
        if not await abort_session(session, row, storage_root, ErrorCode.UPLOAD_BYTES_MISSING, token=token):
            raise WriterLost()
        raise BytesMissing(ErrorCode.UPLOAD_BYTES_MISSING, session_status=STATUS_ABORTED)

    remaining = expected_size - offset
    written = 0
    renewed_at = utcnow()
    try:
        async with aiofiles.open(part, "ab") as handle:
            if size > offset:
                await handle.truncate(offset)
            async for piece in body:
                if not piece:
                    continue
                written += len(piece)
                if written > max_chunk_bytes:
                    raise ChunkTooLarge(ErrorCode.UPLOAD_CHUNK_TOO_LARGE, max_chunk_bytes=max_chunk_bytes)
                if written > remaining:
                    raise ChunkTooLarge(ErrorCode.UPLOAD_CHUNK_OVERRUN, expected_size=expected_size)
                now = utcnow()
                if now - renewed_at >= timedelta(seconds=WRITER_RENEW_SECONDS):
                    if not await renew_writer(session, upload_id, token):
                        raise WriterLost()
                    renewed_at = now
                await handle.write(piece)
                # Nothing may wait in a buffer: closing the file after a lost claim must not write it
                await handle.flush()
        new_offset = offset + written
        if not await release_writer(session, upload_id, token, new_offset):
            raise WriterLost()
    except WriterLost:
        raise
    except BaseException:
        # A failed flush or commit leaves the session unusable until it is rolled back
        await session.rollback()
        # Renewing checks the claim and keeps it for WRITER_STALE_SECONDS: no one else can write meanwhile
        if await renew_writer(session, upload_id, token):
            try:
                async with aiofiles.open(part, "r+b") as handle:
                    await handle.truncate(offset)
            except FileNotFoundError:
                pass
            await release_writer(session, upload_id, token, offset)
        raise
    return new_offset


async def release_writer(session: AsyncSession, upload_id: str, token: str, received_bytes: int) -> bool:
    """Record received_bytes and free the session, only while this writer holds it"""
    now = utcnow()
    return await _update(session, upload_id, now, UploadSession.status == STATUS_OPEN, UploadSession.writer_token == token,
                         writer_token=None, writer_since=None, received_bytes=received_bytes)


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


async def claim_completion(session: AsyncSession, upload_id: str) -> Optional[str]:
    """Take a fully received session for completion: status `completing`, writer_token held by the caller.

    Only from open without a live chunk writer, or over a completion claim that went stale (its request died).
    None when refused: the caller reads the row to say why.
    """
    now = utcnow()
    token = uuid.uuid4().hex
    claimable = ((UploadSession.status == STATUS_OPEN) & _writer_free(now)) | _completion_stale(now)
    claimed = await _update(
        session, upload_id, now, claimable, UploadSession.received_bytes == UploadSession.expected_size,
        status=STATUS_COMPLETING, writer_token=token, writer_since=now,
    )
    return token if claimed else None


async def release_completion(session: AsyncSession, upload_id: str, token: str) -> bool:
    """Back to open after a failure that is not the file's (busy storage, an unexpected error); False when the
    claim is no longer held (the session was finished, aborted or taken over)"""
    now = utcnow()
    return await _update(session, upload_id, now, _holds_completion(token),
                         status=STATUS_OPEN, writer_token=None, writer_since=None)


async def finish_completion(session: AsyncSession, upload_id: str, token: str, media_file_id: int) -> bool:
    """The file is stored as media_file_id: the session is completed, while the claim is still held"""
    now = utcnow()
    return await _update(session, upload_id, now, _holds_completion(token),
                         status=STATUS_COMPLETED, media_file_id=media_file_id, completed_at=now, error=None,
                         writer_token=None, writer_since=None)


async def completed_media(session: AsyncSession, row: UploadSession) -> MediaFile:
    """The media file a completed session stored"""
    return (await session.exec(select(MediaFile).where(MediaFile.id == row.media_file_id))).one()


async def abort_session(
    session: AsyncSession, row: UploadSession, storage_root: Path,
    code: Optional[ErrorCode] = None, token: Optional[str] = None,
) -> bool:
    """End the session as aborted and delete its part file.

    `code` is the problem that ended it (None: the client gave up). With a token only while that claim (a chunk
    writer or the completion) holds the session; without, when it is open or its completion claim went stale.
    False when the session was in neither state; nothing is changed then.
    """
    now = utcnow()
    held = (UploadSession.writer_token == token) & UploadSession.status.in_((STATUS_OPEN, STATUS_COMPLETING)) \
        if token else _endable(now)
    if not await _update(session, row.id, now, held, status=STATUS_ABORTED, error=code.value if code else None,
                         writer_token=None, writer_since=None):
        return False
    await remove_part(row, storage_root)
    return True


async def remove_part(row: UploadSession, storage_root: Path) -> None:
    part = resolve_part(row, storage_root)
    try:
        await aiofiles.os.remove(part)
    except FileNotFoundError:
        pass


def is_expired(row: UploadSession, now: Optional[datetime] = None) -> bool:
    return _aware(row.expires_at) <= (now or utcnow())


async def expire_if_due(session: AsyncSession, row: UploadSession, storage_root: Path, now: Optional[datetime] = None) -> bool:
    """An open session past expires_at becomes expired and loses its part file; a completion that is running is
    left to finish (a stale completion claim expires). True when this call expired it."""
    now = now or utcnow()
    if row.status not in (STATUS_OPEN, STATUS_COMPLETING) or not is_expired(row, now):
        return False
    if not await _update(session, row.id, now, _endable(now), status=STATUS_EXPIRED, error=None,
                         writer_token=None, writer_since=None):
        return False
    await remove_part(row, storage_root)
    return True


async def expire_sessions(session: AsyncSession, storage_root: Path, now: Optional[datetime] = None) -> Dict[str, int]:
    """Sessions past expires_at become expired and lose their part file (see expire_if_due); finished sessions
    older than twice the ttl lose a leftover part file too (rows are kept for the audit trail)."""
    now = now or utcnow()
    counts = {"expired": 0, "cleaned": 0}
    rows = (await session.exec(
        select(UploadSession).where(UploadSession.status.in_((STATUS_OPEN, STATUS_COMPLETING)))
    )).all()
    for row in rows:
        if await expire_if_due(session, row, storage_root, now):
            counts["expired"] += 1
    cutoff = now - timedelta(hours=2 * settings.storage.upload.session_ttl_hours)
    finished = (await session.exec(
        select(UploadSession).where(
            UploadSession.status.in_((STATUS_COMPLETED, STATUS_ABORTED, STATUS_EXPIRED)), UploadSession.updated_at < cutoff)
    )).all()
    for row in finished:
        part = resolve_part(row, storage_root)
        if part.exists():
            await remove_part(row, storage_root)
            counts["cleaned"] += 1
    return counts


def error_code(row: UploadSession) -> Optional[str]:
    """`error` as answered: the problem code that ended the session, or None"""
    return row.error if row.error in _PROBLEM_CODES else None


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
    skipped: int = 0


def sweep_nginx_temp(directory: Path, max_age_seconds: float, now: Optional[float] = None) -> SweepResult:
    """Delete files under `directory` not modified for `max_age_seconds`; directories stay (nginx owns them).

    A file that cannot be read or deleted (PermissionError or another OSError) is logged, counted in `skipped` and
    left; the sweep goes on with the next one."""
    root = Path(directory)
    if not root.is_dir():
        return SweepResult(0, 0)
    cutoff = (time.time() if now is None else now) - max_age_seconds
    files = freed = skipped = 0
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
        except OSError as exc:
            skipped += 1
            _logger.warning(f"sweep-nginx-temp: skipped {path}: {exc!r}")
            continue
        files += 1
        freed += info.st_size
    return SweepResult(files, freed, skipped)
