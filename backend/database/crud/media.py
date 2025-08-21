"""
Media file CRUD operations for file management and chunked uploads.
Handles file metadata, chunked uploads, and integrity verification.
"""

import hashlib
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from loguru import logger
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from backend.database.crud.base import CRUDBase
from backend.models.media import MediaFile, FileChunk, MediaType
from backend.schemas.media import MediaFileCreate, MediaFileUpdate


class CRUDMediaFile(CRUDBase[MediaFile, MediaFileCreate, MediaFileUpdate]):
    """CRUD operations for MediaFile model."""

    async def get_by_observation(self, db: AsyncSession, observation_id: int) -> List[MediaFile]:
        """Get all media files for an observation."""
        try:
            query = (
                select(MediaFile)
                .where(MediaFile.observation_id == observation_id)
                .order_by(MediaFile.uploaded_at.desc())
            )
            result = await db.execute(query)
            return result.scalars().all()
        except Exception as e:
            logger.error(f"Error getting media files for observation {observation_id}: {e}")
            raise

    async def get_by_hash(self, db: AsyncSession, file_hash: str) -> Optional[MediaFile]:
        """Get media file by hash (for duplicate detection)."""
        try:
            query = select(MediaFile).where(MediaFile.file_hash == file_hash)
            result = await db.execute(query)
            return result.scalar_one_or_none()
        except Exception as e:
            logger.error(f"Error getting media file by hash {file_hash}: {e}")
            raise

    async def get_by_media_type(
            self,
            db: AsyncSession,
            media_type: MediaType,
            observation_id: Optional[int] = None
    ) -> List[MediaFile]:
        """Get media files by type."""
        try:
            query = select(MediaFile).where(MediaFile.media_type == media_type)

            if observation_id:
                query = query.where(MediaFile.observation_id == observation_id)

            query = query.order_by(MediaFile.uploaded_at.desc())

            result = await db.execute(query)
            return result.scalars().all()
        except Exception as e:
            logger.error(f"Error getting media files by type {media_type}: {e}")
            raise

    async def create_with_metadata(
            self,
            db: AsyncSession,
            observation_id: int,
            original_filename: str,
            stored_filename: str,
            file_path: str,
            file_data: bytes,
            mime_type: str,
            media_type: MediaType,
            **kwargs
    ) -> MediaFile:
        """Create media file with automatic metadata extraction."""
        try:
            # Calculate file hash
            file_hash = hashlib.sha256(file_data).hexdigest()
            file_size = len(file_data)

            # Extract file extension
            file_extension = Path(original_filename).suffix.lower()

            # Create media file record
            media_data = {
                "observation_id": observation_id,
                "original_filename": original_filename,
                "stored_filename": stored_filename,
                "file_path": file_path,
                "file_size": file_size,
                "mime_type": mime_type,
                "file_extension": file_extension,
                "media_type": media_type,
                "file_hash": file_hash,
                **kwargs
            }

            # Extract media-specific metadata
            if media_type == MediaType.IMAGE:
                metadata = await self._extract_image_metadata(file_data)
                media_data.update(metadata)
            elif media_type == MediaType.VIDEO:
                metadata = await self._extract_video_metadata(file_data)
                media_data.update(metadata)
            elif media_type == MediaType.AUDIO:
                metadata = await self._extract_audio_metadata(file_data)
                media_data.update(metadata)

            db_media = MediaFile(**media_data)
            db.add(db_media)
            await db.commit()
            await db.refresh(db_media)

            logger.info(f"Created media file {db_media.id} for observation {observation_id}")
            return db_media

        except Exception as e:
            await db.rollback()
            logger.error(f"Error creating media file: {e}")
            raise

    async def _extract_image_metadata(self, file_data: bytes) -> Dict:
        """Extract metadata from image files."""
        try:
            from PIL import Image
            import io

            with Image.open(io.BytesIO(file_data)) as img:
                width, height = img.size
                return {"width": width, "height": height}
        except Exception as e:
            logger.warning(f"Could not extract image metadata: {e}")
            return {}

    async def _extract_video_metadata(self, file_data: bytes) -> Dict:
        """Extract metadata from video files."""
        # Note: This would require a video processing library like ffmpeg-python
        # For now, return empty dict
        logger.debug("Video metadata extraction not implemented")
        return {}

    async def _extract_audio_metadata(self, file_data: bytes) -> Dict:
        """Extract metadata from audio files."""
        # Note: This would require an audio processing library
        # For now, return empty dict
        logger.debug("Audio metadata extraction not implemented")
        return {}

    async def get_storage_statistics(self, db: AsyncSession) -> Dict[str, any]:
        """Get file storage statistics."""
        try:
            stats = {}

            # Total files and size
            query = select(
                func.count(MediaFile.id),
                func.sum(MediaFile.file_size)
            )
            result = await db.execute(query)
            total_count, total_size = result.first()

            stats['total_files'] = total_count or 0
            stats['total_size_bytes'] = total_size or 0

            # Count by media type
            for media_type in MediaType:
                query = select(func.count()).where(MediaFile.media_type == media_type)
                result = await db.execute(query)
                stats[f'{media_type.value}_count'] = result.scalar()

            # Size by media type
            for media_type in MediaType:
                query = select(func.sum(MediaFile.file_size)).where(MediaFile.media_type == media_type)
                result = await db.execute(query)
                stats[f'{media_type.value}_size'] = result.scalar() or 0

            # Files with processing errors
            query = select(func.count()).where(MediaFile.processing_error.isnot(None))
            result = await db.execute(query)
            stats['error_count'] = result.scalar()

            return stats

        except Exception as e:
            logger.error(f"Error getting storage statistics: {e}")
            raise

    async def cleanup_orphaned_files(self, db: AsyncSession) -> int:
        """Remove media file records for files that no longer exist on disk."""
        try:
            query = select(MediaFile)
            result = await db.execute(query)
            media_files = result.scalars().all()

            orphaned_count = 0
            for media_file in media_files:
                if not Path(media_file.file_path).exists():
                    await db.delete(media_file)
                    orphaned_count += 1

            if orphaned_count > 0:
                await db.commit()
                logger.info(f"Cleaned up {orphaned_count} orphaned media file records")

            return orphaned_count

        except Exception as e:
            await db.rollback()
            logger.error(f"Error cleaning up orphaned files: {e}")
            raise


class CRUDFileChunk(CRUDBase[FileChunk, dict, dict]):
    """CRUD operations for FileChunk model."""

    async def create_chunk(
            self,
            db: AsyncSession,
            media_file_id: int,
            chunk_number: int,
            chunk_data: bytes
    ) -> FileChunk:
        """Create a file chunk with integrity verification."""
        try:
            # Calculate chunk hash
            chunk_hash = hashlib.sha256(chunk_data).hexdigest()
            chunk_size = len(chunk_data)

            chunk = FileChunk(
                media_file_id=media_file_id,
                chunk_number=chunk_number,
                chunk_size=chunk_size,
                chunk_hash=chunk_hash,
                chunk_data=chunk_data
            )

            db.add(chunk)
            await db.commit()
            await db.refresh(chunk)

            # Verify chunk integrity immediately
            await self.verify_chunk_integrity(db, chunk.id)

            logger.debug(f"Created chunk {chunk_number} for media file {media_file_id}")
            return chunk

        except Exception as e:
            await db.rollback()
            logger.error(f"Error creating file chunk: {e}")
            raise

    async def get_chunks_for_file(
            self,
            db: AsyncSession,
            media_file_id: int
    ) -> List[FileChunk]:
        """Get all chunks for a media file, ordered by chunk number."""
        try:
            query = (
                select(FileChunk)
                .where(FileChunk.media_file_id == media_file_id)
                .order_by(FileChunk.chunk_number)
            )
            result = await db.execute(query)
            return result.scalars().all()
        except Exception as e:
            logger.error(f"Error getting chunks for media file {media_file_id}: {e}")
            raise

    async def verify_chunk_integrity(self, db: AsyncSession, chunk_id: int) -> bool:
        """Verify chunk data integrity."""
        try:
            chunk = await self.get(db, chunk_id)
            if not chunk:
                return False

            is_valid = chunk.verify_chunk()
            await db.commit()  # Save verification result

            if not is_valid:
                logger.warning(f"Chunk {chunk_id} failed integrity verification")

            return is_valid

        except Exception as e:
            logger.error(f"Error verifying chunk {chunk_id}: {e}")
            raise

    async def reassemble_file(
            self,
            db: AsyncSession,
            media_file_id: int
    ) -> Optional[bytes]:
        """Reassemble file from chunks."""
        try:
            chunks = await self.get_chunks_for_file(db, media_file_id)
            if not chunks:
                return None

            # Verify all chunks are present and in order
            expected_chunks = list(range(len(chunks)))
            actual_chunks = [chunk.chunk_number for chunk in chunks]

            if expected_chunks != sorted(actual_chunks):
                logger.error(f"Missing or out-of-order chunks for media file {media_file_id}")
                return None

            # Verify chunk integrity
            for chunk in chunks:
                if not chunk.is_verified:
                    is_valid = await self.verify_chunk_integrity(db, chunk.id)
                    if not is_valid:
                        logger.error(f"Chunk {chunk.id} integrity verification failed")
                        return None

            # Reassemble file data
            file_data = b''.join(chunk.chunk_data for chunk in chunks)

            logger.debug(f"Reassembled file from {len(chunks)} chunks")
            return file_data

        except Exception as e:
            logger.error(f"Error reassembling file from chunks: {e}")
            raise

    async def cleanup_incomplete_uploads(
            self,
            db: AsyncSession,
            age_hours: int = 24
    ) -> int:
        """Clean up chunks from incomplete uploads older than specified age."""
        try:
            from datetime import datetime, timedelta

            cutoff_time = datetime.utcnow() - timedelta(hours=age_hours)

            # Find media files that have chunks but are not marked as processed
            query = (
                select(MediaFile.id)
                .join(FileChunk)
                .where(
                    and_(
                        MediaFile.is_processed == False,
                        FileChunk.uploaded_at < cutoff_time
                    )
                )
                .distinct()
            )

            result = await db.execute(query)
            incomplete_file_ids = result.scalars().all()

            if not incomplete_file_ids:
                return 0

            # Delete chunks for incomplete files
            from sqlalchemy import delete

            chunk_delete_query = delete(FileChunk).where(
                FileChunk.media_file_id.in_(incomplete_file_ids)
            )
            chunk_result = await db.execute(chunk_delete_query)

            # Delete incomplete media file records
            file_delete_query = delete(MediaFile).where(
                MediaFile.id.in_(incomplete_file_ids)
            )
            file_result = await db.execute(file_delete_query)

            await db.commit()

            cleaned_count = file_result.rowcount
            logger.info(f"Cleaned up {cleaned_count} incomplete uploads and their chunks")

            return cleaned_count

        except Exception as e:
            await db.rollback()
            logger.error(f"Error cleaning up incomplete uploads: {e}")
            raise


# Create instances
media_file = CRUDMediaFile(MediaFile)
file_chunk = CRUDFileChunk(FileChunk)
