import os
import asyncio
import shutil
import hashlib
import uuid
import re
import unicodedata
from pathlib import Path
from typing import Optional, Dict, Any, List, Set
from dataclasses import dataclass
from datetime import datetime, timezone
from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
import aiofiles
import aiofiles.os
from pydantic import BaseModel, field_validator

from app.backend.models.media import MediaFile
from app.backend.services.media.jobs import enqueue_poster, release_poster_hold
from app.backend.services.media.formats import UnsupportedMediaFormat, FileTooLarge
from app.backend.services.media.probe import image_dimensions
from app.backend.enums.general import MediaType
from app.backend.utils.logging_config import get_logger
from app.backend.utils.mime_detector import mime_detector
from app.backend.errors.coded import CodedError
from app.backend.enums.errors import ErrorCode

_logger = get_logger("media_manager")

# Security and validation configurations
MAX_FILE_SIZE = 500 * 1024 * 1024  # 500MB
MAX_FILENAME_LENGTH = 255
# Accepted formats live in app/backend/services/media/formats.py (single registry)

DANGEROUS_EXTENSIONS = {
    '.exe', '.bat', '.cmd', '.com', '.pif', '.scr', '.vbs', '.js', '.jar',
    '.app', '.deb', '.pkg', '.dmg', '.iso', '.msi', '.run', '.deb', '.rpm'
}


class FileValidationConfig(BaseModel):
    max_size: int = MAX_FILE_SIZE
    allowed_extensions: Set[str] = set()
    allowed_mime_types: Set[str] = set()
    validate_content_headers: bool = True
    sanitize_filename: bool = True
    strict_mime_validation: bool = True

    @field_validator('allowed_extensions', mode='before')
    @classmethod
    def validate_extensions(cls, v):
        if isinstance(v, (list, tuple)):
            return set(ext.lower() for ext in v)
        return v or set()

    @field_validator('allowed_mime_types', mode='before')
    @classmethod
    def validate_mime_types(cls, v):
        if isinstance(v, (list, tuple)):
            return set(v)
        return v or set()


@dataclass
class FileValidationResult:
    is_valid: bool
    issues: List[str]
    sanitized_filename: Optional[str] = None
    detected_mime_type: Optional[str] = None
    file_size: Optional[int] = None
    metadata: Dict[str, Any] = None


@dataclass
class FileIntegrityCheck:
    media_file_id: int
    original_filename: str
    expected_size: int
    filesystem_exists: bool
    filesystem_size: Optional[int]
    hash_matches: bool
    status: str  # 'ok', 'missing', 'corrupted'
    issues: List[str]
    security_validated: bool = True


def _move_into_place(source: Path, destination: Path) -> None:
    """Rename on the same filesystem, copy and delete across filesystems (blocking: run it in a thread)"""
    try:
        source.rename(destination)
    except OSError:
        shutil.copy2(str(source), str(destination))
        source.unlink()
    os.chmod(destination, 0o644)


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
                 auto_cleanup: bool = True,
                 enable_deduplication: bool = True,
                 _max_concurrent_uploads: int = 10,
                 enable_nginx: bool = False):
        self.storage_path = Path(storage_path)
        self.temp_path = Path(temp_path)
        self.auto_cleanup = auto_cleanup
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
        """Enhanced filename sanitization for cross-platform compatibility"""
        if not filename:
            return f"upload_{uuid.uuid4().hex[:8]}"

        # The extension comes from the original name (as check_declared_format reads it); only the
        # stem is reduced to ASCII, so a stem without ASCII letters cannot swallow the extension
        name, ext = os.path.splitext(filename)
        name, ext = (
            unicodedata.normalize('NFKD', part).encode('ascii', 'ignore').decode('ascii')
            for part in (name, ext)
        )

        # Remove dangerous characters and patterns (Windows + Unix) + spaces
        dangerous_chars = r'[<>:"/\\|?*\x00-\x1f\x7f-\x9f\s]'
        name = re.sub(dangerous_chars, '_', name)
        ext = re.sub(dangerous_chars, '_', ext)

        # Remove multiple dots and normalize
        name = re.sub(r'\.{2,}', '_', name)
        name = re.sub(r'[_]{2,}', '_', name)  # Normalize multiple underscores

        # Remove leading/trailing problematic characters
        name = name.strip('. _-')
        ext = ext.strip('. _-')

        # Ensure extension starts with dot if present
        if ext and not ext.startswith('.'):
            ext = f'.{ext}'

        # Handle reserved Windows filenames
        reserved_names = {
            'CON', 'PRN', 'AUX', 'NUL', 'COM1', 'COM2', 'COM3', 'COM4', 'COM5',
            'COM6', 'COM7', 'COM8', 'COM9', 'LPT1', 'LPT2', 'LPT3', 'LPT4',
            'LPT5', 'LPT6', 'LPT7', 'LPT8', 'LPT9'
        }
        if name.upper() in reserved_names:
            name = f"{name}_file"

        # Reconstruct filename
        sanitized = f"{name}{ext}" if name else ext

        # Apply length limits (account for full path later)
        max_filename_len = min(MAX_FILENAME_LENGTH, 200)  # Conservative limit
        if len(sanitized) > max_filename_len:
            # Preserve extension, truncate name
            if ext:
                max_name_len = max_filename_len - len(ext) - 1
                name = name[:max_name_len]
                sanitized = f"{name}{ext}"
            else:
                sanitized = sanitized[:max_filename_len]

        # Final validation: an empty stem becomes a generated one, the extension is kept
        if not name or name in ['.', '..', '_']:
            sanitized = f"upload_{uuid.uuid4().hex[:8]}{ext}"

        return sanitized

    def validate_full_path_length(self, full_path: str) -> bool:
        """Validate that the full file path doesn't exceed filesystem limits"""
        max_path_length = 250  # Conservative limit
        return len(str(full_path)) <= max_path_length

    def _validate_content_headers(self, file_data: bytes, mime_type: str) -> List[str]:
        """Validate file headers match MIME type"""
        issues = []

        if len(file_data) < 16:
            return issues  # Too small to validate headers

        header = file_data[:16]

        # Common file type validations
        if mime_type.startswith('image/'):
            if not self._is_valid_image_header(header, mime_type):
                issues.append(f"Invalid image header for MIME type {mime_type}")
        elif mime_type.startswith('video/'):
            if not self._is_valid_video_header(header, mime_type):
                issues.append(f"Invalid video header for MIME type {mime_type}")

        return issues

    def _is_valid_image_header(self, header: bytes, mime_type: str) -> bool:
        """Validate image file headers"""
        if mime_type == 'image/jpeg':
            return header.startswith(b'\xff\xd8\xff')
        elif mime_type == 'image/png':
            return header.startswith(b'\x89PNG\r\n\x1a\n')
        elif mime_type == 'image/gif':
            return header.startswith(b'GIF87a') or header.startswith(b'GIF89a')
        elif mime_type == 'image/webp':
            return b'WEBP' in header[:12]
        return True  # Allow other image types

    def _is_valid_video_header(self, header: bytes, mime_type: str) -> bool:
        """Validate video file headers"""
        if mime_type == 'video/mp4':
            return b'ftyp' in header[:16]
        elif mime_type == 'video/webm':
            return header.startswith(b'\x1a\x45\xdf\xa3')
        return True  # Allow other video types

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

    async def check_file_integrity(self, session: AsyncSession, media_file_id: int) -> FileIntegrityCheck:
        """Check file integrity on filesystem"""

        # Get media file info
        media_query = select(MediaFile).where(MediaFile.id == media_file_id)
        media_result = await session.exec(media_query)
        media_file = media_result.first()

        if not media_file:
            return FileIntegrityCheck(
                media_file_id=media_file_id,
                original_filename="Unknown",
                expected_size=0,
                filesystem_exists=False,
                filesystem_size=None,
                hash_matches=False,
                status="missing",
                issues=["Media file record not found in database"],
                security_validated=False
            )

        # Check filesystem
        filesystem_exists = False
        filesystem_size = None
        hash_matches = False
        issues = []

        file_path = media_file.resolve_path(self.storage_path)
        filesystem_exists = file_path.exists() and file_path.is_file()

        if filesystem_exists:
            try:
                filesystem_size = file_path.stat().st_size
            except OSError:
                filesystem_size = None

            # Verify hash
            try:
                file_hash = await self._compute_file_hash(file_path)
                hash_matches = file_hash == media_file.file_hash
            except OSError as e:
                issues.append(f"Error during integrity check: {str(e)}")

            if not hash_matches:
                issues.append("File hash mismatch - data corruption detected")

            if filesystem_size != media_file.file_size:
                issues.append(f"Filesystem size mismatch: expected {media_file.file_size}, got {filesystem_size}")
        else:
            issues.append("Filesystem file missing")

        # Determine overall status
        if not issues:
            status = "ok"
        elif not filesystem_exists:
            status = "missing"
        elif not hash_matches:
            status = "corrupted"
        else:
            status = "warning"

        return FileIntegrityCheck(
            media_file_id=media_file_id,
            original_filename=media_file.original_filename,
            expected_size=media_file.file_size,
            filesystem_exists=filesystem_exists,
            filesystem_size=filesystem_size,
            hash_matches=hash_matches,
            status=status,
            issues=issues,
            security_validated=not bool(issues)
        )

    async def repair_file(self, session: AsyncSession, media_file_id: int) -> bool:
        """Attempt to repair a corrupted or missing file (no-op if file is intact)"""

        integrity = await self.check_file_integrity(session, media_file_id)

        if integrity.status == "ok":
            _logger.info(f"File {media_file_id} is already intact")
            return True

        _logger.warning(f"Could not repair file {media_file_id} - no backup source available")
        return False

    async def cleanup_orphaned_files(self, session: AsyncSession) -> tuple[int, int]:
        """Clean up orphaned files and database records"""

        db_cleaned = 0
        fs_cleaned = 0

        # Find orphaned database records (files without observation)
        if self.auto_cleanup:
            from app.backend.models.observation import Observation

            orphaned_query = select(MediaFile).join(
                Observation, isouter=True
            ).where(
                MediaFile.observation_id == None,
                MediaFile.is_deleted == False
            )

            orphaned_result = await session.exec(orphaned_query)
            orphaned_files = orphaned_result.all()

            for media_file in orphaned_files:
                _logger.info(f"Cleaning up orphaned media file: {media_file.original_filename}")

                # Delete filesystem file if exists
                try:
                    file_path = media_file.resolve_path(self.storage_path)
                    if file_path.exists():
                        file_path.unlink()
                        fs_cleaned += 1
                except OSError as e:
                    _logger.warning(f"Could not delete orphaned filesystem file {media_file.file_path}: {e}")

                # Delete media file record
                await session.delete(media_file)
                db_cleaned += 1

            if db_cleaned > 0:
                await session.commit()

        # Clean up orphaned filesystem files
        if self.storage_path.exists():
            try:
                for observation_dir in self.storage_path.iterdir():
                    if observation_dir.is_dir():
                        for file_path in observation_dir.iterdir():
                            if file_path.is_file():
                                # Check if file exists in database
                                filename = file_path.name
                                file_query = select(MediaFile).where(
                                    MediaFile.stored_filename == filename
                                )
                                file_result = await session.exec(file_query)

                                if not file_result.first():
                                    _logger.info(f"Removing orphaned filesystem file: {file_path}")
                                    file_path.unlink()
                                    fs_cleaned += 1

                        # Remove empty directories
                        if not any(observation_dir.iterdir()):
                            observation_dir.rmdir()

            except OSError as e:
                # A filesystem failure stops this pass; a database error propagates
                _logger.error(f"Error during filesystem cleanup: {e}")

        _logger.info(f"Cleanup completed: {db_cleaned} database records, {fs_cleaned} filesystem files")
        return db_cleaned, fs_cleaned

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

    async def _detect_mime_from_path(self, file_path: Path, filename: str) -> str:
        """Detect MIME type from file header without loading the entire file"""
        async with aiofiles.open(file_path, 'rb') as f:
            header = await f.read(8192)
        detected_mime, _ = mime_detector.detect_mime_type(header, filename)
        return detected_mime

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
        """Copy a Starlette UploadFile to a temp file in 1 MB chunks so the body never sits in memory.

        The caller passes the returned path to store_file_from_path, which moves
        it into storage, and removes it on failure.
        """
        self.temp_path.mkdir(parents=True, exist_ok=True)
        suffix = Path(upload.filename or "").suffix.lower()[:10]
        target = self.temp_path / f"upload_{uuid.uuid4().hex}{suffix}"
        async with aiofiles.open(target, 'wb') as out:
            while True:
                chunk = await upload.read(chunk_size)
                if not chunk:
                    break
                await out.write(chunk)
        return target

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
        MIME type is detected from the first 8KB, and the file is moved (not copied)
        to its final storage location.
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
            file_ext = Path(safe_filename).suffix.lower()

            if file_ext in DANGEROUS_EXTENSIONS:
                raise UnsupportedMediaFormat(ErrorCode.MEDIA_EXTENSION_DANGEROUS, media_type, extension=file_ext)
            if config.allowed_extensions and file_ext not in config.allowed_extensions:
                raise UnsupportedMediaFormat(ErrorCode.MEDIA_EXTENSION_NOT_ACCEPTED, media_type, extension=file_ext)

            # -- Media type validation --
            try:
                media_type_enum = MediaType(media_type.lower())
            except ValueError:
                raise UnsupportedMediaFormat(ErrorCode.MEDIA_TYPE_UNKNOWN, media_type)

            # -- MIME detection (first 8KB only) --
            mime_type = await self._detect_mime_from_path(source, original_filename)
            if config.allowed_mime_types and mime_type not in config.allowed_mime_types:
                raise UnsupportedMediaFormat(ErrorCode.MEDIA_CONTENT_TYPE_NOT_ACCEPTED, media_type, content_type=mime_type)

            # -- Content header validation (first 16 bytes) --
            if config.validate_content_headers:
                async with aiofiles.open(source, 'rb') as f:
                    header_bytes = await f.read(16)
                content_issues = self._validate_content_headers(header_bytes, mime_type)
                if content_issues:
                    _logger.warning(f"Content validation failed for {original_filename!r}: {'; '.join(content_issues)}")
                    raise CodedError(ErrorCode.MEDIA_CONTENT_INVALID)

            # -- Hash computation (streaming, 1MB chunks) --
            file_hash = await self._compute_file_hash(source)

            # -- Image dimensions (header only) --
            dimensions = None
            if mime_type.startswith('image/'):
                dimensions = await self._extract_image_dimensions(source)

            # -- Deduplication --
            if self.enable_deduplication:
                existing_id = await self._check_for_duplicate(session, file_hash, observation_id)
                if existing_id:
                    _logger.info(f"Duplicate file detected, returning existing ID: {existing_id}")
                    source.unlink(missing_ok=True)
                    return existing_id

            # -- Prepare storage path --
            organized_dir = await self._get_organized_path(session, observation_id)
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            temp_hash = hashlib.md5(f"{safe_filename}_{timestamp}".encode()).hexdigest()[:16]
            temp_stored_filename = f"temp_{temp_hash}{file_ext}"

            # -- Create DB record with temp filename --
            media_file = MediaFile(
                observation_id=observation_id,
                user_id=user_id,
                original_filename=safe_filename,
                stored_filename=temp_stored_filename,
                file_path=str((organized_dir / temp_stored_filename).relative_to(self.storage_path)),
                file_size=file_size,
                mime_type=mime_type,
                file_extension=file_ext,
                media_type=media_type_enum.value,
                file_hash=file_hash
            )
            if dimensions:
                media_file.width, media_file.height = dimensions

            # Video posters are extracted by the media worker; queue in the creating transaction, held until the
            # file is in storage (released below, in the commit after the move)
            enqueue_poster(media_file)

            session.add(media_file)
            await session.commit()
            await session.refresh(media_file)

            # -- Generate final filename with DB id --
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

            if media_file.is_video:
                # The file is in storage: the worker may take the poster job from now on
                await release_poster_hold(session, media_file.id)
                await session.refresh(media_file)

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
