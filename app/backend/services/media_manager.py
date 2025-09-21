import os
import asyncio
import hashlib
import shutil
import tempfile
import uuid
import re
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple, Set, Union
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
import aiofiles
import aiofiles.os
from pydantic import BaseModel, field_validator

from app.backend.models.media import MediaFile, FileChunk
from app.backend.enums.general import MediaType
from app.backend.utils.logging_config import get_logger
from app.backend.utils.mime_detector import mime_detector

logger = get_logger("media_manager")

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
    actual_size: int
    expected_chunks: int
    actual_chunks: int
    missing_chunks: List[int]
    filesystem_exists: bool
    filesystem_size: Optional[int]
    hash_matches: bool
    status: str  # 'ok', 'incomplete', 'missing', 'corrupted', 'orphaned'
    issues: List[str]
    security_validated: bool = True


class MediaManager:
    """Enterprise-grade media file manager with security, validation, and performance optimizations"""

    def __init__(self,
                 storage_path: str = "uploads",
                 chunk_size: int = 1024 * 1024,  # 1MB chunks
                 use_filesystem_cache: bool = True,
                 auto_cleanup: bool = True,
                 temp_path: str = "temp",
                 enable_deduplication: bool = True,
                 _max_concurrent_uploads: int = 10):
        self.storage_path = Path(storage_path)
        self.temp_path = Path(temp_path)
        self.chunk_size = chunk_size
        self.use_filesystem_cache = use_filesystem_cache
        self.auto_cleanup = auto_cleanup
        self.enable_deduplication = enable_deduplication

        # Ensure directories exist with proper permissions
        for path in [self.storage_path, self.temp_path]:
            path.mkdir(parents=True, exist_ok=True, mode=0o755)

        # Rate limiting and concurrency control
        self._upload_sessions: Dict[str, datetime] = {}
        self._max_concurrent_uploads = _max_concurrent_uploads
        self._active_uploads: Set[str] = set()

        # File deduplication cache
        self._hash_cache: Dict[str, int] = {}
    
    def sanitize_filename(self, filename: str) -> str:
        """Sanitize filename to prevent security issues"""
        # Remove dangerous characters and patterns
        filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
        filename = re.sub(r'\.{2,}', '.', filename)  # Remove multiple dots
        filename = filename.strip('. ')  # Remove leading/trailing dots and spaces

        # Limit length
        if len(filename) > MAX_FILENAME_LENGTH:
            name, ext = os.path.splitext(filename)
            max_name_len = MAX_FILENAME_LENGTH - len(ext)
            filename = name[:max_name_len] + ext

        # Ensure not empty
        if not filename or filename in ['.', '..']:
            filename = f"upload_{uuid.uuid4().hex[:8]}"

        return filename

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
            logger.warning(f"Failed to extract metadata: {e}")

        return metadata

    async def store_file_chunked(self,
                               session: AsyncSession,
                               observation_id: int,
                               file_data: bytes,
                               original_filename: str,
                               media_type: str,
                               validation_config: Optional[FileValidationConfig] = None) -> int:
        """Store file in database chunks with comprehensive validation and security checks"""

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
                    logger.info(f"Duplicate file detected, returning existing file ID: {existing_file_id}")
                    return existing_file_id
        
            # Generate human-readable stored filename (simpler for manual access)
            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            stored_filename = f"{timestamp}_{safe_filename}"

            # Use validated MIME type
            mime_type = validation_result.detected_mime_type
            logger.info(f"Validated MIME type for {safe_filename}: {mime_type}")

            # Use date-based organized directory structure for human readability
            organized_dir = await self._get_organized_path(session, observation_id)

            # Create media file record with organized path
            media_file = MediaFile(
                observation_id=observation_id,
                original_filename=safe_filename,
                stored_filename=stored_filename,
                file_path=str(organized_dir / stored_filename),
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

            # Store file in chunks with optimized processing
            total_chunks = (file_size + self.chunk_size - 1) // self.chunk_size

            # Use batch processing for large files
            chunk_batch = []
            batch_size = 50  # Process chunks in batches

            for chunk_number in range(total_chunks):
                start_offset = chunk_number * self.chunk_size
                end_offset = min(start_offset + self.chunk_size, file_size)
                chunk_data = file_data[start_offset:end_offset]
                chunk_hash = hashlib.sha256(chunk_data).hexdigest()

                # Create chunk file path using organized structure
                chunk_filename = f"{media_file.id}_chunk_{chunk_number:04d}.bin"
                await aiofiles.os.makedirs(organized_dir, exist_ok=True)
                chunk_file_path = organized_dir / chunk_filename

                # Save chunk to filesystem
                async with aiofiles.open(chunk_file_path, 'wb') as f:
                    await f.write(chunk_data)

                file_chunk = FileChunk(
                    media_file_id=media_file.id,
                    chunk_number=chunk_number,
                    chunk_size=len(chunk_data),
                    chunk_hash=chunk_hash,
                    chunk_file_path=str(chunk_file_path)
                )

                chunk_batch.append(file_chunk)

                # Commit chunks in batches for better performance
                if len(chunk_batch) >= batch_size or chunk_number == total_chunks - 1:
                    for chunk in chunk_batch:
                        session.add(chunk)
                    await session.commit()
                    chunk_batch = []

            # Update deduplication cache
            if self.enable_deduplication:
                self._hash_cache[file_hash] = media_file.id
        
            # Optionally create filesystem backup
            if self.use_filesystem_cache:
                await self._create_filesystem_backup(media_file.id, file_data, organized_dir, stored_filename)

                # After successful backup creation, clean up individual chunk files
                await self._cleanup_chunk_files(organized_dir, media_file.id, total_chunks)

            logger.info(f"Stored file {safe_filename} as {stored_filename} with {total_chunks} chunks")
            return media_file.id

        finally:
            # Always remove from active uploads
            self._active_uploads.discard(upload_session_id)
    
    async def _check_for_duplicate(self, session: AsyncSession, file_hash: str, observation_id: int) -> Optional[int]:
        """Check if file already exists based on hash"""
        # Check cache first
        if file_hash in self._hash_cache:
            # Verify the file still exists in database
            media_query = select(MediaFile).where(
                MediaFile.id == self._hash_cache[file_hash],
                MediaFile.observation_id == observation_id
            )
            result = await session.exec(media_query)
            if result.first():
                return self._hash_cache[file_hash]
            else:
                # Remove stale cache entry
                del self._hash_cache[file_hash]

        # Check database
        duplicate_query = select(MediaFile).where(
            MediaFile.file_hash == file_hash,
            MediaFile.observation_id == observation_id
        )
        result = await session.exec(duplicate_query)
        duplicate = result.first()

        if duplicate:
            self._hash_cache[file_hash] = duplicate.id
            return duplicate.id

        return None

    async def _get_organized_path(self, session: AsyncSession, observation_id: int, observed_at: str = None) -> Path:
        """Generate organized directory path based on date and observation info"""
        # Use current date for organization since we're having import issues
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
        """Create filesystem backup of the file with async I/O"""
        try:
            await aiofiles.os.makedirs(organized_dir, exist_ok=True)

            file_path = organized_dir / stored_filename

            # Use async file I/O for better performance
            async with aiofiles.open(file_path, 'wb') as f:
                await f.write(file_data)

            # Set secure file permissions
            os.chmod(file_path, 0o644)

            logger.debug(f"Created filesystem backup: {file_path}")
            
        except Exception as e:
            logger.error(f"Failed to create filesystem backup for media_file {media_file_id}: {e}")

    async def _cleanup_chunk_files(self, organized_dir: Path, media_file_id: int, total_chunks: int):
        """Clean up individual chunk files after successful complete file creation"""
        try:
            for chunk_number in range(total_chunks):
                chunk_filename = f"{media_file_id}_chunk_{chunk_number:04d}.bin"
                chunk_file_path = organized_dir / chunk_filename

                if chunk_file_path.exists():
                    chunk_file_path.unlink()
                    logger.debug(f"Cleaned up chunk file: {chunk_file_path}")

            logger.info(f"Cleaned up {total_chunks} chunk files for media_file {media_file_id}")

        except Exception as e:
            logger.warning(f"Failed to cleanup chunk files for media_file {media_file_id}: {e}")

    async def retrieve_file(self, session: AsyncSession, media_file_id: int,
                          validate_integrity: bool = True) -> Optional[bytes]:
        """Retrieve complete file from database chunks with integrity validation and fallback"""
        
        # Get media file info
        media_query = select(MediaFile).where(MediaFile.id == media_file_id)
        media_result = await session.exec(media_query)
        media_file = media_result.first()
        
        if not media_file:
            logger.error(f"Media file {media_file_id} not found")
            return None
        
        # Try to retrieve from database chunks first
        try:
            chunks_query = select(FileChunk).where(
                FileChunk.media_file_id == media_file_id
            ).order_by(FileChunk.chunk_number)

            chunks_result = await session.exec(chunks_query)
            chunks = chunks_result.all()

            expected_chunks = (media_file.file_size + self.chunk_size - 1) // self.chunk_size

            if len(chunks) == expected_chunks:
                # Read chunk data from filesystem and verify integrity
                chunk_data_list = []
                for chunk in chunks:
                    # Read chunk from filesystem
                    chunk_path = Path(chunk.chunk_file_path)
                    if not chunk_path.exists():
                        logger.error(f"Chunk file not found: {chunk_path}")
                        raise ValueError(f"Chunk file {chunk.chunk_number} not found")

                    async with aiofiles.open(chunk_path, 'rb') as f:
                        chunk_data = await f.read()

                    # Verify chunk integrity if requested
                    if validate_integrity:
                        chunk_hash = hashlib.sha256(chunk_data).hexdigest()
                        if chunk_hash != chunk.chunk_hash:
                            logger.error(f"Chunk {chunk.chunk_number} hash mismatch for file {media_file_id}")
                            raise ValueError(f"Chunk integrity validation failed")

                    chunk_data_list.append(chunk_data)

                # Reassemble from chunks
                file_data = b"".join(chunk_data_list)

                # Verify file integrity
                if validate_integrity:
                    file_hash = hashlib.sha256(file_data).hexdigest()
                    if file_hash != media_file.file_hash:
                        logger.error(f"File hash mismatch for file {media_file_id}")
                        raise ValueError("File integrity validation failed")

                logger.debug(f"Retrieved file {media_file_id} from database chunks")
                return file_data
            else:
                logger.warning(f"Incomplete chunks for file {media_file_id}: {len(chunks)}/{expected_chunks}")
        
        except Exception as e:
            logger.error(f"Failed to retrieve file {media_file_id} from database: {e}")
        
        # Fallback to filesystem if available
        if self.use_filesystem_cache:
            return await self._retrieve_from_filesystem(media_file, validate_integrity)
        
        return None
    
    async def _retrieve_from_filesystem(self, media_file: MediaFile,
                                      validate_integrity: bool = True) -> Optional[bytes]:
        """Retrieve file from filesystem backup with async I/O"""
        try:
            file_path = Path(media_file.file_path)

            if file_path.exists() and file_path.is_file():
                # Use async file I/O
                async with aiofiles.open(file_path, 'rb') as f:
                    file_data = await f.read()

                # Verify file integrity if requested
                if validate_integrity:
                    file_hash = hashlib.sha256(file_data).hexdigest()
                    if file_hash != media_file.file_hash:
                        logger.error(f"Hash mismatch for filesystem file {media_file.file_path}")
                        raise ValueError("Filesystem file integrity validation failed")

                logger.info(f"Retrieved file {media_file.id} from filesystem backup")
                return file_data
            else:
                logger.warning(f"Filesystem file not found: {file_path}")
        
        except Exception as e:
            logger.error(f"Failed to retrieve file from filesystem {media_file.file_path}: {e}")
        
        return None
    
    async def check_file_integrity(self, session: AsyncSession, media_file_id: int) -> FileIntegrityCheck:
        """Comprehensive file integrity check"""
        
        # Get media file info
        media_query = select(MediaFile).where(MediaFile.id == media_file_id)
        media_result = await session.exec(media_query)
        media_file = media_result.first()
        
        if not media_file:
            return FileIntegrityCheck(
                media_file_id=media_file_id,
                original_filename="Unknown",
                expected_size=0,
                actual_size=0,
                expected_chunks=0,
                actual_chunks=0,
                missing_chunks=[],
                filesystem_exists=False,
                filesystem_size=None,
                hash_matches=False,
                status="missing",
                issues=["Media file record not found in database"],
                security_validated=False
            )
        
        # Get chunks info
        chunks_query = select(FileChunk).where(
            FileChunk.media_file_id == media_file_id
        ).order_by(FileChunk.chunk_number)
        
        chunks_result = await session.exec(chunks_query)
        chunks = chunks_result.all()
        
        expected_chunks = (media_file.file_size + self.chunk_size - 1) // self.chunk_size
        actual_chunks = len(chunks)
        actual_size = sum(chunk.chunk_size for chunk in chunks)
        
        # Find missing chunks
        existing_chunk_numbers = set(chunk.chunk_number for chunk in chunks)
        expected_chunk_numbers = set(range(expected_chunks))
        missing_chunks = list(expected_chunk_numbers - existing_chunk_numbers)
        
        # Check filesystem
        filesystem_exists = False
        filesystem_size = None
        
        if self.use_filesystem_cache:
            file_path = Path(media_file.file_path)
            filesystem_exists = file_path.exists() and file_path.is_file()
            if filesystem_exists:
                try:
                    filesystem_size = file_path.stat().st_size
                except Exception:
                    filesystem_size = None
        
        # Check hash integrity if all chunks available
        hash_matches = False
        security_validated = True
        issues = []  # Initialize issues list here

        if actual_chunks == expected_chunks and not missing_chunks:
            try:
                # Verify individual chunk hashes first and collect data
                chunk_data_list = []
                for chunk in chunks:
                    chunk_path = Path(chunk.chunk_file_path)
                    if not chunk_path.exists():
                        security_validated = False
                        issues.append(f"Chunk file {chunk.chunk_number} not found: {chunk_path}")
                        continue

                    async with aiofiles.open(chunk_path, 'rb') as f:
                        chunk_data = await f.read()

                    chunk_hash = hashlib.sha256(chunk_data).hexdigest()
                    if chunk_hash != chunk.chunk_hash:
                        security_validated = False
                        issues.append(f"Chunk {chunk.chunk_number} hash mismatch")

                    chunk_data_list.append(chunk_data)

                if security_validated and len(chunk_data_list) == expected_chunks:
                    file_data = b"".join(chunk_data_list)
                    file_hash = hashlib.sha256(file_data).hexdigest()
                    hash_matches = file_hash == media_file.file_hash
            except Exception as e:
                hash_matches = False
                security_validated = False
                issues.append(f"Error during integrity check: {str(e)}")

        # Add additional issues
        
        if missing_chunks:
            issues.append(f"Missing {len(missing_chunks)} chunks: {missing_chunks[:5]}{'...' if len(missing_chunks) > 5 else ''}")
        
        if actual_size != media_file.file_size:
            issues.append(f"Size mismatch: expected {media_file.file_size}, got {actual_size}")
        
        if not hash_matches and not missing_chunks:
            issues.append("File hash mismatch - data corruption detected")
        
        if self.use_filesystem_cache and not filesystem_exists:
            issues.append("Filesystem backup missing")
        elif filesystem_exists and filesystem_size != media_file.file_size:
            issues.append(f"Filesystem size mismatch: expected {media_file.file_size}, got {filesystem_size}")
        
        # Determine overall status
        if not issues:
            status = "ok"
        elif missing_chunks:
            status = "incomplete"
        elif not hash_matches:
            status = "corrupted"
        else:
            status = "warning"
        
        return FileIntegrityCheck(
            media_file_id=media_file_id,
            original_filename=media_file.original_filename,
            expected_size=media_file.file_size,
            actual_size=actual_size,
            expected_chunks=expected_chunks,
            actual_chunks=actual_chunks,
            missing_chunks=missing_chunks,
            filesystem_exists=filesystem_exists,
            filesystem_size=filesystem_size,
            hash_matches=hash_matches,
            status=status,
            issues=issues,
            security_validated=security_validated
        )
    
    async def repair_file(self, session: AsyncSession, media_file_id: int) -> bool:
        """Attempt to repair a corrupted or incomplete file"""
        
        integrity = await self.check_file_integrity(session, media_file_id)
        
        if integrity.status == "ok":
            logger.info(f"File {media_file_id} is already intact")
            return True
        
        # Try to restore from filesystem backup
        if integrity.filesystem_exists and integrity.status in ["incomplete", "corrupted"]:
            logger.info(f"Attempting to restore file {media_file_id} from filesystem backup")
            
            media_query = select(MediaFile).where(MediaFile.id == media_file_id)
            media_result = await session.exec(media_query)
            media_file = media_result.first()
            
            if media_file:
                try:
                    # Read filesystem file with async I/O
                    file_path = Path(media_file.file_path)
                    async with aiofiles.open(file_path, 'rb') as f:
                        file_data = await f.read()
                    
                    # Verify hash
                    file_hash = hashlib.sha256(file_data).hexdigest()
                    if file_hash != media_file.file_hash:
                        logger.error(f"Filesystem backup hash mismatch for file {media_file_id}")
                        return False
                    
                    # Delete existing chunks
                    chunks_query = select(FileChunk).where(FileChunk.media_file_id == media_file_id)
                    chunks_result = await session.exec(chunks_query)
                    chunks = chunks_result.all()
                    
                    for chunk in chunks:
                        await session.delete(chunk)
                    
                    # Recreate chunks from filesystem file
                    total_chunks = (len(file_data) + self.chunk_size - 1) // self.chunk_size
                    
                    for chunk_number in range(total_chunks):
                        start_offset = chunk_number * self.chunk_size
                        end_offset = min(start_offset + self.chunk_size, len(file_data))
                        chunk_data = file_data[start_offset:end_offset]
                        chunk_hash = hashlib.sha256(chunk_data).hexdigest()
                        
                        # Create chunk file path for repair
                        chunk_filename = f"{media_file_id}_chunk_{chunk_number:04d}.bin"
                        observation_dir = self.storage_path / str(media_file.observation_id)
                        await aiofiles.os.makedirs(observation_dir, exist_ok=True)
                        chunk_file_path = observation_dir / chunk_filename

                        # Save chunk to filesystem
                        async with aiofiles.open(chunk_file_path, 'wb') as f:
                            await f.write(chunk_data)

                        file_chunk = FileChunk(
                            media_file_id=media_file_id,
                            chunk_number=chunk_number,
                            chunk_size=len(chunk_data),
                            chunk_hash=chunk_hash,
                            chunk_file_path=str(chunk_file_path)
                        )
                        
                        session.add(file_chunk)
                    
                    await session.commit()
                    logger.info(f"Successfully repaired file {media_file_id} from filesystem backup")
                    return True
                
                except Exception as e:
                    logger.error(f"Failed to repair file {media_file_id} from filesystem: {e}")
                    await session.rollback()
        
        logger.warning(f"Could not repair file {media_file_id}")
        return False
    
    async def cleanup_orphaned_files(self, session: AsyncSession) -> Tuple[int, int]:
        """Clean up orphaned files and database records"""
        
        db_cleaned = 0
        fs_cleaned = 0
        
        # Find orphaned database records (files without observation)
        if self.auto_cleanup:
            from app.backend.models.observation import Observation

            orphaned_query = select(MediaFile).join(
                Observation, isouter=True
            ).where(MediaFile.observation_id is None)
            
            orphaned_result = await session.exec(orphaned_query)
            orphaned_files = orphaned_result.all()
            
            for media_file in orphaned_files:
                logger.info(f"Cleaning up orphaned media file: {media_file.original_filename}")
                
                # Delete chunks
                chunks_query = select(FileChunk).where(FileChunk.media_file_id == media_file.id)
                chunks_result = await session.exec(chunks_query)
                chunks = chunks_result.all()
                
                for chunk in chunks:
                    await session.delete(chunk)
                
                # Delete filesystem file if exists
                if self.use_filesystem_cache:
                    try:
                        file_path = Path(media_file.file_path)
                        if file_path.exists():
                            file_path.unlink()
                            fs_cleaned += 1
                    except Exception as e:
                        logger.warning(f"Could not delete orphaned filesystem file {media_file.file_path}: {e}")
                
                # Delete media file record
                await session.delete(media_file)
                db_cleaned += 1
            
            if db_cleaned > 0:
                await session.commit()
        
        # Clean up orphaned filesystem files
        if self.use_filesystem_cache and self.storage_path.exists():
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
                                    logger.info(f"Removing orphaned filesystem file: {file_path}")
                                    file_path.unlink()
                                    fs_cleaned += 1
                        
                        # Remove empty directories
                        if not any(observation_dir.iterdir()):
                            observation_dir.rmdir()
            
            except Exception as e:
                logger.error(f"Error during filesystem cleanup: {e}")
        
        logger.info(f"Cleanup completed: {db_cleaned} database records, {fs_cleaned} filesystem files")
        return db_cleaned, fs_cleaned

    async def cleanup_legacy_chunk_files(self, session: AsyncSession) -> int:
        """Clean up chunk files that exist on filesystem but have corresponding complete files"""
        chunks_cleaned = 0

        if not self.use_filesystem_cache or not self.storage_path.exists():
            return 0

        try:
            # Get all media files that have complete filesystem backups
            media_query = select(MediaFile)
            media_result = await session.exec(media_query)
            all_media_files = media_result.all()

            for media_file in all_media_files:
                # Check if complete file exists on filesystem
                complete_file_path = Path(media_file.file_path)
                if complete_file_path.exists() and complete_file_path.is_file():
                    # Look for chunk files for this media file
                    observation_dir = self.storage_path / str(media_file.observation_id)
                    if observation_dir.exists():
                        chunk_pattern = f"{media_file.id}_chunk_*.bin"
                        chunk_files = list(observation_dir.glob(chunk_pattern))

                        if chunk_files:
                            logger.info(f"Cleaning up {len(chunk_files)} legacy chunk files for media_file {media_file.id}")

                            for chunk_file in chunk_files:
                                try:
                                    chunk_file.unlink()
                                    chunks_cleaned += 1
                                    logger.debug(f"Removed legacy chunk file: {chunk_file}")
                                except Exception as e:
                                    logger.warning(f"Failed to remove chunk file {chunk_file}: {e}")

            logger.info(f"Legacy chunk cleanup completed: {chunks_cleaned} chunk files removed")

        except Exception as e:
            logger.error(f"Error during legacy chunk cleanup: {e}")

        return chunks_cleaned

    async def get_storage_statistics(self, session: AsyncSession) -> Dict[str, Any]:
        """Get storage statistics and health information"""
        
        # Database statistics
        files_query = select(MediaFile)
        files_result = await session.exec(files_query)
        all_files = files_result.all()
        
        chunks_query = select(FileChunk)
        chunks_result = await session.exec(chunks_query)
        all_chunks = chunks_result.all()
        
        total_files = len(all_files)
        total_chunks = len(all_chunks)
        total_db_size = sum(chunk.chunk_size for chunk in all_chunks)
        
        # File type breakdown
        media_types = {}
        for file in all_files:
            media_types[file.media_type] = media_types.get(file.media_type, 0) + 1
        
        # Filesystem statistics
        fs_stats = {}
        if self.use_filesystem_cache and self.storage_path.exists():
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
                "filesystem_enabled": True,
                "filesystem_files": fs_files,
                "filesystem_size_bytes": fs_size,
                "storage_path": str(self.storage_path)
            }
        else:
            fs_stats = {"filesystem_enabled": False}
        
        return {
            "total_files": total_files,
            "total_chunks": total_chunks,
            "total_database_size_bytes": total_db_size,
            "media_types": media_types,
            "chunk_size": self.chunk_size,
            **fs_stats
        }