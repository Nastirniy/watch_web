import os
import shutil
import hashlib
import uuid
import re
import unicodedata
from pathlib import Path
from typing import Optional, Dict, Any, List, Set
from dataclasses import dataclass
from datetime import datetime, timezone
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
import aiofiles
import aiofiles.os
from pydantic import BaseModel, field_validator

from app.backend.models.media import MediaFile
from app.backend.enums.general import MediaType
from app.backend.utils.logging_config import get_logger
from app.backend.utils.mime_detector import mime_detector

_logger = get_logger("media_manager")

# Security and validation configurations
MAX_FILE_SIZE = 500 * 1024 * 1024  # 500MB
MAX_FILENAME_LENGTH = 255
ALLOWED_EXTENSIONS = {
    'image': {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.tiff'},
    'video': {'.mp4', '.avi', '.mkv', '.mov', '.wmv', '.flv', '.webm'},
    'audio': {'.mp3', '.wav', '.flac', '.aac', '.ogg', '.m4a'},
    'document': {'.pdf', '.doc', '.docx', '.txt', '.rtf'},
    'archive': {'.zip', '.rar', '.7z', '.tar', '.gz'}
}

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


class MediaManager:
    """Media file manager with security, validation, and filesystem storage"""

    def __init__(self,
                 storage_path: str = "uploads",
                 use_filesystem_cache: bool = True,
                 auto_cleanup: bool = True,
                 temp_path: str = "temp",
                 enable_deduplication: bool = True,
                 _max_concurrent_uploads: int = 10,
                 nginx_symlink_path: Optional[str] = None,
                 enable_nginx: bool = False):
        self.storage_path = Path(storage_path)
        self.temp_path = Path(temp_path)
        self.use_filesystem_cache = use_filesystem_cache
        self.auto_cleanup = auto_cleanup
        self.enable_deduplication = enable_deduplication
        self.nginx_symlink_path = Path(nginx_symlink_path) if nginx_symlink_path else None
        self.enable_nginx = enable_nginx

        # Ensure directories exist with proper permissions
        for path in [self.storage_path, self.temp_path]:
            path.mkdir(parents=True, exist_ok=True, mode=0o755)

        # Create Nginx symlink directory if enabled
        if self.enable_nginx and self.nginx_symlink_path:
            try:
                self.nginx_symlink_path.mkdir(parents=True, exist_ok=True, mode=0o755)
                _logger.info(f"Nginx symlink directory initialized at: {self.nginx_symlink_path}")
            except Exception as e:
                _logger.error(f"Failed to create Nginx symlink directory: {e}")
                self.enable_nginx = False

        # Rate limiting and concurrency control
        self._upload_sessions: Dict[str, datetime] = {}
        self._max_concurrent_uploads = _max_concurrent_uploads
        self._active_uploads: Set[str] = set()

        # File deduplication cache
        self._hash_cache: Dict[str, int] = {}

    def sanitize_filename(self, filename: str) -> str:
        """Enhanced filename sanitization for cross-platform compatibility"""
        if not filename:
            return f"upload_{uuid.uuid4().hex[:8]}"

        # Normalize Unicode characters (decompose accented characters)
        filename = unicodedata.normalize('NFKD', filename)

        # Convert to ASCII, replacing non-ASCII characters
        filename = filename.encode('ascii', 'ignore').decode('ascii')

        # Split name and extension for proper handling
        name, ext = os.path.splitext(filename)

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

        # Final validation
        if not sanitized or sanitized in ['.', '..', '_']:
            sanitized = f"upload_{uuid.uuid4().hex[:8]}{ext}"

        return sanitized

    def validate_full_path_length(self, full_path: str) -> bool:
        """Validate that the full file path doesn't exceed filesystem limits"""
        max_path_length = 250  # Conservative limit
        return len(str(full_path)) <= max_path_length

    async def validate_file(self, file_data: bytes, filename: str,
                          config: Optional[FileValidationConfig] = None) -> FileValidationResult:
        """Comprehensive file validation"""
        issues = []
        config = config or FileValidationConfig()

        # Size validation
        file_size = len(file_data)
        if file_size > config.max_size:
            issues.append(f"File size {file_size} exceeds maximum {config.max_size}")

        if file_size == 0:
            issues.append("File is empty")

        # Filename validation and sanitization
        sanitized_filename = self.sanitize_filename(filename) if config.sanitize_filename else filename

        # Extension validation
        file_ext = Path(filename).suffix.lower()
        if file_ext in DANGEROUS_EXTENSIONS:
            issues.append(f"Dangerous file extension: {file_ext}")

        if config.allowed_extensions and file_ext not in config.allowed_extensions:
            issues.append(f"File extension {file_ext} not allowed")

        # MIME type detection and validation
        detected_mime, detection_method = mime_detector.detect_mime_type(file_data, filename)

        if config.allowed_mime_types and detected_mime not in config.allowed_mime_types:
            issues.append(f"MIME type {detected_mime} not allowed")

        # Content header validation
        if config.validate_content_headers:
            content_issues = self._validate_content_headers(file_data, detected_mime)
            issues.extend(content_issues)

        # Extract metadata safely
        metadata = await self._extract_safe_metadata(file_data, detected_mime)

        return FileValidationResult(
            is_valid=len(issues) == 0,
            issues=issues,
            sanitized_filename=sanitized_filename,
            detected_mime_type=detected_mime,
            file_size=file_size,
            metadata=metadata
        )

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

    async def _extract_safe_metadata(self, file_data: bytes, mime_type: str) -> Dict[str, Any]:
        """Safely extract metadata without executing code"""
        metadata = {}

        try:
            if mime_type.startswith('image/'):
                # Use PIL for safe image metadata extraction
                import io
                from PIL import Image

                with Image.open(io.BytesIO(file_data)) as img:
                    metadata.update({
                        'width': img.width,
                        'height': img.height,
                        'format': img.format,
                        'mode': img.mode
                    })

                    # Safely extract EXIF data (without executing)
                    if hasattr(img, '_getexif') and img._getexif():
                        exif = img._getexif()
                        if exif:
                            # Only extract safe EXIF tags
                            safe_tags = {256: 'width', 257: 'height', 272: 'make', 282: 'x_resolution'}
                            metadata['exif'] = {safe_tags.get(k, k): v for k, v in exif.items()
                                              if k in safe_tags and isinstance(v, (str, int, float))}
        except Exception as e:
            _logger.warning(f"Failed to extract metadata: {e}")

        return metadata

    async def store_file(self,
                        session: AsyncSession,
                        observation_id: int,
                        file_data: bytes,
                        original_filename: str,
                        media_type: str,
                        user_id: Optional[int] = None,
                        validation_config: Optional[FileValidationConfig] = None) -> int:
        """Store file on filesystem with comprehensive validation and security checks"""

        # Generate unique upload session ID
        upload_session_id = str(uuid.uuid4())

        try:
            # Rate limiting check
            if len(self._active_uploads) >= self._max_concurrent_uploads:
                raise ValueError("Too many concurrent uploads. Please try again later.")

            self._active_uploads.add(upload_session_id)

            # Comprehensive file validation
            validation_result = await self.validate_file(file_data, original_filename, validation_config)
            if not validation_result.is_valid:
                raise ValueError(f"File validation failed: {'; '.join(validation_result.issues)}")

            # Use sanitized filename
            safe_filename = validation_result.sanitized_filename or original_filename

            file_size = validation_result.file_size
            file_extension = Path(safe_filename).suffix or ''

            # Validate media type
            try:
                media_type_enum = MediaType(media_type.lower())
            except ValueError:
                raise ValueError(f"Invalid media type: {media_type}")

            # Generate file hash for deduplication
            file_hash = hashlib.sha256(file_data).hexdigest()

            # Check for duplicate files if deduplication is enabled
            if self.enable_deduplication:
                existing_file_id = await self._check_for_duplicate(session, file_hash, observation_id)
                if existing_file_id:
                    _logger.info(f"Duplicate file detected, returning existing file ID: {existing_file_id}")
                    return existing_file_id

            # Use validated MIME type
            mime_type = validation_result.detected_mime_type
            _logger.info(f"Validated MIME type for {safe_filename}: {mime_type}")

            # Use date-based organized directory structure for human readability
            organized_dir = await self._get_organized_path(session, observation_id)

            # Generate temporary stored filename using unique hash
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            temp_hash = hashlib.md5(f"{safe_filename}_{timestamp}".encode()).hexdigest()[:16]
            temp_stored_filename = f"temp_{temp_hash}{file_extension}"

            # Create media file record with temporary filename
            media_file = MediaFile(
                observation_id=observation_id,
                user_id=user_id,
                original_filename=safe_filename,
                stored_filename=temp_stored_filename,
                file_path=str((organized_dir / temp_stored_filename).relative_to(self.storage_path)),
                file_size=file_size,
                mime_type=mime_type,
                file_extension=file_extension,
                media_type=media_type_enum.value,
                file_hash=file_hash
            )

            # Add metadata if available
            if validation_result.metadata:
                if 'width' in validation_result.metadata:
                    media_file.width = validation_result.metadata['width']
                if 'height' in validation_result.metadata:
                    media_file.height = validation_result.metadata['height']

            session.add(media_file)
            await session.commit()
            await session.refresh(media_file)

            # Generate final stored filename using media_file.id and MD5 hash of original filename
            md5_hash = hashlib.md5(safe_filename.encode()).hexdigest()[:16]
            stored_filename = f"{media_file.id}_{md5_hash}{file_extension}"
            full_file_path = organized_dir / stored_filename

            # Update media file with final filename
            media_file.stored_filename = stored_filename
            media_file.file_path = str(full_file_path.relative_to(self.storage_path))
            await session.commit()
            await session.refresh(media_file)

            # Save file to filesystem — if this fails, delete the DB record
            try:
                await self._create_filesystem_backup(media_file.id, file_data, organized_dir, stored_filename)
            except Exception as e:
                _logger.error(f"File write failed for media_file {media_file.id}, rolling back DB record: {e}")
                await session.delete(media_file)
                await session.commit()
                raise ValueError(f"Failed to store file on filesystem: {e}") from e

            # Update deduplication cache
            if self.enable_deduplication:
                self._hash_cache[file_hash] = media_file.id

            _logger.info(f"Stored file {safe_filename} as {stored_filename}")
            return media_file.id

        finally:
            # Always remove from active uploads
            self._active_uploads.discard(upload_session_id)

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

    async def _create_filesystem_backup(self,
                                      media_file_id: int,
                                      file_data: bytes,
                                      organized_dir: Path,
                                      stored_filename: str):
        """Save file to filesystem with async I/O. Raises on failure so the caller can roll back the DB record."""
        await aiofiles.os.makedirs(organized_dir, exist_ok=True)
        file_path = organized_dir / stored_filename
        async with aiofiles.open(file_path, 'wb') as f:
            await f.write(file_data)
        os.chmod(file_path, 0o644)
        _logger.debug(f"Saved file to filesystem: {file_path}")

    async def retrieve_file(self, session: AsyncSession, media_file_id: int,
                          validate_integrity: bool = True) -> Optional[bytes]:
        """Retrieve file from filesystem with integrity validation"""

        # Get media file info
        media_query = select(MediaFile).where(MediaFile.id == media_file_id)
        media_result = await session.exec(media_query)
        media_file = media_result.first()

        if not media_file:
            _logger.error(f"Media file {media_file_id} not found")
            return None

        return await self._retrieve_from_filesystem(media_file, validate_integrity)

    async def _retrieve_from_filesystem(self, media_file: MediaFile,
                                      validate_integrity: bool = True) -> Optional[bytes]:
        """Retrieve file from filesystem with async I/O"""
        try:
            file_path = media_file.resolve_path(self.storage_path)

            if file_path.exists() and file_path.is_file():
                # Use async file I/O
                async with aiofiles.open(file_path, 'rb') as f:
                    file_data = await f.read()

                # Verify file integrity if requested
                if validate_integrity:
                    file_hash = hashlib.sha256(file_data).hexdigest()
                    if file_hash != media_file.file_hash:
                        _logger.error(f"Hash mismatch for filesystem file {media_file.file_path}")
                        raise ValueError("Filesystem file integrity validation failed")

                _logger.info(f"Retrieved file {media_file.id} from filesystem")
                return file_data
            else:
                _logger.warning(f"Filesystem file not found: {file_path}")

        except Exception as e:
            _logger.error(f"Failed to retrieve file from filesystem {media_file.file_path}: {e}")

        return None

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
            except Exception:
                filesystem_size = None

            # Verify hash
            try:
                async with aiofiles.open(file_path, 'rb') as f:
                    file_data = await f.read()
                file_hash = hashlib.sha256(file_data).hexdigest()
                hash_matches = file_hash == media_file.file_hash
            except Exception as e:
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
                except Exception as e:
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

            except Exception as e:
                _logger.error(f"Error during filesystem cleanup: {e}")

        _logger.info(f"Cleanup completed: {db_cleaned} database records, {fs_cleaned} filesystem files")
        return db_cleaned, fs_cleaned

    async def create_nginx_symlink(self, media_file: MediaFile) -> bool:
        """Create symlink in Nginx directory for video files"""
        if not self.enable_nginx or not self.nginx_symlink_path:
            return False

        if media_file.media_type.lower() != 'video':
            return False

        try:
            source_path = media_file.resolve_path(self.storage_path)
            if not source_path.exists() or not source_path.is_file():
                _logger.warning(f"Source file not found for symlink: {source_path}")
                return False

            symlink_path = self.nginx_symlink_path / media_file.stored_filename

            if symlink_path.exists() or symlink_path.is_symlink():
                try:
                    symlink_path.unlink()
                    _logger.debug(f"Removed existing symlink: {symlink_path}")
                except Exception as e:
                    _logger.warning(f"Failed to remove existing symlink: {e}")

            symlink_path.symlink_to(source_path.resolve())
            _logger.info(f"Created Nginx symlink: {symlink_path} -> {source_path}")
            return True

        except OSError as e:
            _logger.error(f"Failed to create symlink for {media_file.stored_filename}: {e}")
            return False
        except Exception as e:
            _logger.error(f"Unexpected error creating symlink: {e}")
            return False

    async def delete_nginx_symlink(self, media_file: MediaFile) -> bool:
        """Delete symlink from Nginx directory"""
        if not self.enable_nginx or not self.nginx_symlink_path:
            return False

        if media_file.media_type.lower() != 'video':
            return False

        try:
            symlink_path = self.nginx_symlink_path / media_file.stored_filename

            if symlink_path.exists() or symlink_path.is_symlink():
                symlink_path.unlink()
                _logger.info(f"Deleted Nginx symlink: {symlink_path}")
                return True
            else:
                _logger.debug(f"Symlink not found: {symlink_path}")
                return False

        except Exception as e:
            _logger.error(f"Failed to delete symlink for {media_file.stored_filename}: {e}")
            return False

    def get_nginx_symlink_path(self, media_file: MediaFile) -> Optional[str]:
        """Get the path to the Nginx symlink for X-Accel-Redirect"""
        if not self.enable_nginx or not self.nginx_symlink_path:
            return None

        if media_file.media_type.lower() != 'video':
            return None

        return f"/{media_file.stored_filename}"

    async def cleanup_orphaned_symlinks(self) -> int:
        """Clean up broken symlinks in Nginx directory"""
        if not self.enable_nginx or not self.nginx_symlink_path:
            return 0

        if not self.nginx_symlink_path.exists():
            return 0

        cleaned = 0
        try:
            for symlink_path in self.nginx_symlink_path.iterdir():
                if symlink_path.is_symlink():
                    if not symlink_path.exists():
                        symlink_path.unlink()
                        _logger.info(f"Removed broken symlink: {symlink_path}")
                        cleaned += 1

            _logger.info(f"Cleaned up {cleaned} orphaned symlinks")

        except Exception as e:
            _logger.error(f"Error during symlink cleanup: {e}")

        return cleaned

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
        """Move file to storage. Uses rename if same filesystem, copy+delete otherwise."""
        await aiofiles.os.makedirs(destination.parent, exist_ok=True)
        try:
            source.rename(destination)
        except OSError:
            shutil.copy2(str(source), str(destination))
            source.unlink()
        os.chmod(destination, 0o644)

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
        Store a file from a filesystem path (for Nginx upload offloading).
        The file is never loaded entirely into memory -- hash is computed by streaming,
        MIME type is detected from the first 8KB, and the file is moved (not copied)
        to its final storage location.
        """
        upload_session_id = str(uuid.uuid4())

        try:
            if len(self._active_uploads) >= self._max_concurrent_uploads:
                raise ValueError("Too many concurrent uploads. Please try again later.")

            self._active_uploads.add(upload_session_id)

            source = Path(source_path)
            if not source.exists() or not source.is_file():
                raise ValueError(f"Nginx temp file not found at: {source_path}")

            config = validation_config or FileValidationConfig()

            # -- Size validation (stat, no read) --
            file_size = source.stat().st_size
            if file_size > config.max_size:
                raise ValueError(f"File size {file_size} exceeds maximum {config.max_size}")
            if file_size == 0:
                raise ValueError("File is empty")

            # -- Filename sanitization --
            safe_filename = self.sanitize_filename(original_filename) if config.sanitize_filename else original_filename
            file_ext = Path(safe_filename).suffix.lower()

            if file_ext in DANGEROUS_EXTENSIONS:
                raise ValueError(f"Dangerous file extension: {file_ext}")
            if config.allowed_extensions and file_ext not in config.allowed_extensions:
                raise ValueError(f"File extension {file_ext} not allowed")

            # -- Media type validation --
            try:
                media_type_enum = MediaType(media_type.lower())
            except ValueError:
                raise ValueError(f"Invalid media type: {media_type}")

            # -- MIME detection (first 8KB only) --
            mime_type = await self._detect_mime_from_path(source, original_filename)
            if config.allowed_mime_types and mime_type not in config.allowed_mime_types:
                raise ValueError(f"MIME type {mime_type} not allowed")

            # -- Content header validation (first 16 bytes) --
            if config.validate_content_headers:
                async with aiofiles.open(source, 'rb') as f:
                    header_bytes = await f.read(16)
                content_issues = self._validate_content_headers(header_bytes, mime_type)
                if content_issues:
                    raise ValueError(f"Content validation failed: {'; '.join(content_issues)}")

            # -- Hash computation (streaming, 1MB chunks) --
            file_hash = await self._compute_file_hash(source)

            # -- Deduplication --
            if self.enable_deduplication:
                existing_id = await self._check_for_duplicate(session, file_hash, observation_id)
                if existing_id:
                    _logger.info(f"Duplicate file detected, returning existing ID: {existing_id}")
                    try:
                        source.unlink()
                    except Exception:
                        pass
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
                _logger.error(f"File move failed for media_file {media_file.id}, rolling back DB record: {e}")
                await session.delete(media_file)
                await session.commit()
                raise ValueError(f"Failed to move file to storage: {e}") from e

            if self.enable_deduplication:
                self._hash_cache[file_hash] = media_file.id

            _logger.info(f"Stored file from Nginx temp: {safe_filename} -> {stored_filename} ({file_size} bytes)")
            return media_file.id

        finally:
            self._active_uploads.discard(upload_session_id)

    async def get_storage_statistics(self, session: AsyncSession) -> Dict[str, Any]:
        """Get storage statistics and health information"""

        # Database statistics
        files_query = select(MediaFile)
        files_result = await session.exec(files_query)
        all_files = files_result.all()

        active_files = [f for f in all_files if not f.is_deleted]
        deleted_files = [f for f in all_files if f.is_deleted]

        total_files = len(all_files)
        total_file_size = sum(f.file_size for f in all_files)
        
        active_count = len(active_files)
        active_size = sum(f.file_size for f in active_files)
        
        deleted_count = len(deleted_files)
        deleted_size = sum(f.file_size for f in deleted_files)

        # File type breakdown (active only)
        media_types = {}
        for file in active_files:
            media_types[file.media_type] = media_types.get(file.media_type, 0) + 1

        # Filesystem statistics
        fs_stats = {}
        if self.storage_path.exists():
            fs_size = 0
            fs_files = 0

            for root, dirs, files in os.walk(self.storage_path):
                for file in files:
                    file_path = Path(root) / file
                    try:
                        fs_size += file_path.stat().st_size
                        fs_files += 1
                    except Exception:
                        pass

            fs_stats = {
                "filesystem_files": fs_files,
                "filesystem_size_bytes": fs_size,
                "storage_path": str(self.storage_path)
            }

        return {
            "total_files": total_files,
            "total_file_size_bytes": total_file_size,
            "active_count": active_count,
            "active_size_bytes": active_size,
            "deleted_count": deleted_count,
            "deleted_size_bytes": deleted_size,
            "media_types": media_types,
            **fs_stats
        }
