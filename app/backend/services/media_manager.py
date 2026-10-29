import os
import asyncio
import hashlib
import uuid
from pathlib import Path
from typing import Optional, Dict, Any, Set
from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
import aiofiles
import aiofiles.os
from pydantic import BaseModel, Field, field_validator

from app.backend.models.media import MediaFile
from app.backend.models.observation import Observation
from app.backend.models.post import Post
from app.backend.services.media.jobs import enqueue_poster
from app.backend.services.files.formats import UnsupportedMediaFormat, FileTooLarge, content_type_allowed, hard_limit_bytes
from app.backend.services.files.names import DANGEROUS_EXTENSIONS, extension_of, sanitize_filename
from app.backend.services.files.sniff import check_text, detect_content_type, is_text_format
from app.backend.services.files.store import move_into_place as _move_into_place, spool_upload
from app.backend.services.media.probe import image_dimensions
from app.backend.enums.general import MediaType
from app.backend.utils.logging_config import get_logger
from app.backend.errors.coded import CodedError
from app.backend.enums.errors import ErrorCode
from app.backend.utils.row_locks import lock, lock_first

_logger = get_logger("media_manager")

# Accepted formats live in app/backend/services/files/formats.py (single registry), the sniffer in
# services/files/sniff.py; DANGEROUS_EXTENSIONS (services/files/names.py) are refused whatever a format lists


class FileValidationConfig(BaseModel):
    """What store_file_from_path holds a file to, built from its format (upload_helper.validation_config_for)"""
    max_size: int = Field(default_factory=hard_limit_bytes)
    allowed_extensions: Set[str] = set()
    # None: no check (no format given); a set, even empty, is what the format accepts (a legacy row whose only types
    # the registry leaves out accepts nothing, never everything)
    allowed_mime_types: Optional[Set[str]] = None
    # The format's canonical name, stored for an accepted file (the sniffed type, possibly an alias, only decides
    # acceptance); None stores the sniffed type
    content_type: Optional[str] = None
    sanitize_filename: bool = True

    @field_validator('allowed_extensions', mode='before')
    @classmethod
    def validate_extensions(cls, v):
        if isinstance(v, (list, tuple)):
            return set(ext.lower() for ext in v)
        return v or set()

    @field_validator('allowed_mime_types', mode='before')
    @classmethod
    def validate_mime_types(cls, v):
        # Compared with the sniffed type, which is lower case (libmagic says application/CDFV2)
        return None if v is None else {mime.lower() for mime in v}


def _walk_storage(root: Path) -> Optional[tuple[int, int]]:
    """(files, bytes) under root, None when it does not exist (blocking: run it in a thread)"""
    if not root.exists():
        return None
    files = size = 0
    for directory, _, names in os.walk(root):
        for name in names:
            try:
                size += (Path(directory) / name).stat().st_size
                files += 1
            except OSError:  # removed while walking
                pass
    return files, size


class MediaManager:
    """Media file manager with security, validation, and filesystem storage"""

    def __init__(self,
                 storage_path: str = "uploads",
                 temp_path: str = "temp",
                 enable_deduplication: bool = True,
                 _max_concurrent_uploads: int = 10,
                 enable_nginx: bool = False):
        self.storage_path = Path(storage_path)
        self.temp_path = Path(temp_path)
        self.enable_deduplication = enable_deduplication
        self.enable_nginx = enable_nginx

        # Ensure directories exist with proper permissions
        for path in [self.storage_path, self.temp_path]:
            path.mkdir(parents=True, exist_ok=True, mode=0o755)

        # Rate limiting and concurrency control (per process)
        self._max_concurrent_uploads = _max_concurrent_uploads
        self._active_uploads: Set[str] = set()

        # File deduplication cache
        self._hash_cache: Dict[str, int] = {}

    def sanitize_filename(self, filename: str) -> str:
        """Delegates to services.files.names.sanitize_filename (the one implementation)"""
        return sanitize_filename(filename)

    async def _check_for_duplicate(self, session: AsyncSession, file_hash: str, observation_id: int) -> Optional[int]:
        """Check if file already exists based on hash and confirm the file is on disk."""
        # Invalidate cache entry if present
        if file_hash in self._hash_cache:
            del self._hash_cache[file_hash]

        # Check database
        duplicate_query = select(MediaFile).where(
            MediaFile.file_hash == file_hash,
            MediaFile.observation_id == observation_id,
            MediaFile.is_deleted == False
        )
        result = await session.exec(duplicate_query)
        duplicate = result.first()

        if not duplicate:
            return None

        # Confirm the file physically exists — a DB record without a file is not a usable duplicate
        if not duplicate.resolve_path(self.storage_path).exists():
            _logger.warning(
                f"Duplicate hash {file_hash} found in DB (media {duplicate.id}) "
                f"but file is missing on disk — ignoring as duplicate"
            )
            return None

        self._hash_cache[file_hash] = duplicate.id
        return duplicate.id

    async def _get_organized_path(self, session: AsyncSession, observation_id: int, observed_at: str = None) -> Path:
        """Generate organized directory path based on date and observation info"""
        from datetime import datetime, timezone
        date = datetime.now(timezone.utc)

        # Create date-based folder structure: YYYY/MM/DD
        year_folder = f"{date.year:04d}"
        month_folder = f"{date.month:02d}"
        day_folder = f"{date.day:02d}"

        # Create simple observation folder name
        observation_folder = f"obs_{observation_id:03d}"

        # Build the complete organized path
        organized_path = self.storage_path / year_folder / month_folder / day_folder / observation_folder

        return organized_path

    @staticmethod
    async def _lock_parents(session: AsyncSession, observation_id: int) -> None:
        """The first statements of the write that inserts the media row: the post of the observation, then the
        observation, both shared and by their columns only (the Post entity's presence subqueries would take the
        snapshot), as the observation create does (posts before observations). An archive of the observation, of
        its post or of a unit with its posts waits for the insert, or the insert sees it archived: CodedError
        observation_not_found, post_archived (nothing lands in an archived observation, owner decision
        2026-10-29). The insert never meets a parent row changed after the snapshot (MariaDB error 1020)."""
        # An observation never changes its post: read in the transaction lock_first ends
        post_id = (await session.exec(select(Observation.post_id).where(Observation.id == observation_id))).first()
        if post_id is None:
            raise CodedError(ErrorCode.OBSERVATION_NOT_FOUND)
        post = await lock_first(session, select(Post.id, Post.is_deleted).where(Post.id == post_id), shared=True)
        observation = await lock(session, select(Observation.id, Observation.is_deleted)
                                 .where(Observation.id == observation_id), shared=True)
        if observation is None or observation.is_deleted:
            raise CodedError(ErrorCode.OBSERVATION_NOT_FOUND)
        if post is not None and post.is_deleted:
            raise CodedError(ErrorCode.POST_ARCHIVED, post_id=post_id)

    async def _compute_file_hash(self, file_path: Path, chunk_size: int = 1024 * 1024) -> str:
        """Compute SHA256 hash by streaming the file in chunks (default 1MB), never loading entirely into memory"""
        sha256 = hashlib.sha256()
        async with aiofiles.open(file_path, 'rb') as f:
            while True:
                chunk = await f.read(chunk_size)
                if not chunk:
                    break
                sha256.update(chunk)
        return sha256.hexdigest()

    async def _move_file_to_storage(self, source: Path, destination: Path):
        """Move file to storage. Uses rename if same filesystem, copy+delete otherwise; in a thread, never on the
        event loop (a copy of a large file to a network share takes minutes)."""
        await aiofiles.os.makedirs(destination.parent, exist_ok=True)
        await asyncio.to_thread(_move_into_place, source, destination)

    async def _extract_image_dimensions(self, file_path: Path) -> Optional[tuple[int, int]]:
        """Width and height as displayed, from the image header only (no pixel data is decoded)"""
        dimensions = await asyncio.to_thread(image_dimensions, file_path)
        if dimensions is None:
            _logger.warning(f"Could not read image dimensions from {file_path.name}")
        return dimensions

    async def spool_upload(self, upload, chunk_size: int = 1024 * 1024) -> Path:
        """Copy a Starlette UploadFile to a temp file in 1 MB chunks so the body never sits in memory
        (services/files/store.py spool_upload, the one implementation).

        The caller passes the returned path to store_file_from_path, which moves
        it into storage, and removes it on failure.
        """
        return await spool_upload(upload, self.temp_path, chunk_size)

    async def store_file_from_path(
            self,
            session: AsyncSession,
            observation_id: int,
            source_path: str,
            original_filename: str,
            media_type: str,
            user_id: Optional[int] = None,
            validation_config: Optional[FileValidationConfig] = None
    ) -> int:
        """
        Store a file from a filesystem path (Nginx upload offloading, or a multipart body spooled by spool_upload).
        The file is never loaded entirely into memory -- hash is computed by streaming,
        MIME type is detected from the first 8KB (services/files/sniff.py), and the file is moved (not copied)
        to its final storage location. Checks, in order: size, extension, the sniffed type against the format's
        content type and aliases, the text rule for a text/* format. The row stores the format's canonical content
        type (validation_config.content_type).

        The write of the row begins with the post and observation locks (_lock_parents): an archived observation
        is CodedError observation_not_found, an archived post post_archived, whatever the caller checked before.
        Commits the new row before the move (a file in storage always has its row) and deletes it again when the
        move fails. A new video row keeps its poster hold (enqueue_poster): the caller releases it once the file is
        in storage, in its last transaction with the audit row of the upload (record_upload).
        """
        upload_session_id = str(uuid.uuid4())

        try:
            if len(self._active_uploads) >= self._max_concurrent_uploads:
                raise CodedError(ErrorCode.MEDIA_UPLOADS_BUSY)

            self._active_uploads.add(upload_session_id)

            source = Path(source_path)
            if not source.exists() or not source.is_file():
                _logger.warning(f"Upload source file not found at: {source_path}")
                raise CodedError(ErrorCode.MEDIA_UPLOAD_SOURCE_MISSING)

            config = validation_config or FileValidationConfig()

            # -- Size validation (stat, no read) --
            file_size = source.stat().st_size
            if file_size > config.max_size:
                raise FileTooLarge(f"File size {file_size} exceeds the limit of {config.max_size} bytes", config.max_size, file_size)
            if file_size == 0:
                raise CodedError(ErrorCode.MEDIA_FILE_EMPTY)

            # -- Filename sanitization --
            safe_filename = self.sanitize_filename(original_filename) if config.sanitize_filename else original_filename
            file_ext = extension_of(safe_filename)

            if file_ext in DANGEROUS_EXTENSIONS:
                raise UnsupportedMediaFormat(ErrorCode.MEDIA_EXTENSION_DANGEROUS, media_type, extension=file_ext)
            if config.allowed_extensions and file_ext not in config.allowed_extensions:
                raise UnsupportedMediaFormat(ErrorCode.MEDIA_EXTENSION_NOT_ACCEPTED, media_type, extension=file_ext)

            # -- Media type validation --
            try:
                media_type_enum = MediaType(media_type.lower())
            except ValueError:
                raise UnsupportedMediaFormat(ErrorCode.MEDIA_TYPE_UNKNOWN, media_type)

            # -- Content type from the bytes (first 8KB, libmagic): never from the name --
            mime_type = await asyncio.to_thread(detect_content_type, source)
            if config.allowed_mime_types is not None and mime_type not in config.allowed_mime_types:
                raise UnsupportedMediaFormat(ErrorCode.MEDIA_CONTENT_TYPE_NOT_ACCEPTED, media_type, content_type=mime_type)
            if config.content_type and is_text_format(config.content_type):
                await asyncio.to_thread(check_text, source, config.content_type)
            # Stored and served under the format's canonical name (an MKV libmagic calls video/x-matroska is
            # video/matroska), never the detector's alias
            stored_type = config.content_type or mime_type

            # -- Hash computation (streaming, 1MB chunks) --
            file_hash = await self._compute_file_hash(source)

            # -- Image dimensions (header only) --
            dimensions = None
            if content_type_allowed(MediaType.IMAGE.value, stored_type):
                dimensions = await self._extract_image_dimensions(source)

            # -- The parent rows, locked: the write that inserts the row begins here --
            await self._lock_parents(session, observation_id)

            # -- Deduplication --
            if self.enable_deduplication:
                existing_id = await self._check_for_duplicate(session, file_hash, observation_id)
                if existing_id:
                    _logger.info(f"Duplicate file detected, returning existing ID: {existing_id}")
                    source.unlink(missing_ok=True)
                    return existing_id

            # -- Prepare storage path --
            organized_dir = await self._get_organized_path(session, observation_id)
            # stored_filename is unique and the row holds this name until the file is moved: random, never derived
            # from the file name and the clock (two uploads of one name in one second collided)
            temp_stored_filename = f"temp_{uuid.uuid4().hex}{file_ext}"

            # -- Create DB record with temp filename --
            media_file = MediaFile(
                observation_id=observation_id,
                user_id=user_id,
                original_filename=safe_filename,
                stored_filename=temp_stored_filename,
                file_path=str((organized_dir / temp_stored_filename).relative_to(self.storage_path)),
                file_size=file_size,
                mime_type=stored_type,
                file_extension=file_ext,
                media_type=media_type_enum.value,
                file_hash=file_hash
            )
            if dimensions:
                media_file.width, media_file.height = dimensions

            # Video posters are extracted by the media worker; queue in the creating transaction, held until the
            # file is in storage: the caller releases the hold (release_poster_hold) in its transaction after the
            # move, the one that writes the audit row of the upload (record_upload)
            enqueue_poster(media_file)

            session.add(media_file)
            await session.flush()

            # -- Generate final filename with DB id; the row is committed once, with it, before the move --
            md5_hash = hashlib.md5(safe_filename.encode()).hexdigest()[:16]
            stored_filename = f"{media_file.id}_{md5_hash}{file_ext}"
            full_file_path = organized_dir / stored_filename

            media_file.stored_filename = stored_filename
            media_file.file_path = str(full_file_path.relative_to(self.storage_path))
            await session.commit()
            await session.refresh(media_file)

            # -- Move file from Nginx temp to final storage — if this fails, delete the DB record --
            try:
                await self._move_file_to_storage(source, full_file_path)
            except Exception as e:
                # Whatever failed, the record must not point at a file that is not there; only a
                # filesystem error is the storage refusing the file, anything else stays a 500
                _logger.error(f"File move failed for media_file {media_file.id}, rolling back DB record: {e!r}")
                await session.delete(media_file)
                await session.commit()
                if isinstance(e, OSError):
                    raise CodedError(ErrorCode.MEDIA_STORE_FAILED) from e
                raise

            if self.enable_deduplication:
                self._hash_cache[file_hash] = media_file.id

            _logger.info(f"Stored file from Nginx temp: {safe_filename} -> {stored_filename} ({file_size} bytes)")
            return media_file.id

        finally:
            self._active_uploads.discard(upload_session_id)

    async def get_storage_statistics(self, session: AsyncSession) -> Dict[str, Any]:
        """Counts and sizes of the stored media: SQL aggregates, and a walk of the storage directory in a thread"""
        size_sum = func.coalesce(func.sum(MediaFile.file_size), 0)
        groups = (await session.exec(
            select(MediaFile.is_deleted, MediaFile.media_type, func.count(), size_sum)
            .group_by(MediaFile.is_deleted, MediaFile.media_type)
        )).all()
        active_count = active_size = deleted_count = deleted_size = 0
        # File type breakdown (active only)
        media_types: Dict[str, int] = {}
        for is_deleted, media_type, count, size in groups:
            if is_deleted:
                deleted_count += count
                deleted_size += int(size)
            else:
                active_count += count
                active_size += int(size)
                media_types[media_type] = count

        # Filesystem statistics; the absolute storage path is a server detail: never part of a response
        fs_stats = {}
        walked = await asyncio.to_thread(_walk_storage, self.storage_path)
        if walked is not None:
            fs_stats = {"filesystem_files": walked[0], "filesystem_size_bytes": walked[1]}

        return {
            "total_files": active_count + deleted_count,
            "total_file_size_bytes": active_size + deleted_size,
            "active_count": active_count,
            "active_size_bytes": active_size,
            "deleted_count": deleted_count,
            "deleted_size_bytes": deleted_size,
            "media_types": media_types,
            **fs_stats
        }
