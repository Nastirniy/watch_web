"""
Thumbnail generation service for images and videos.

Handles thumbnail creation with PIL for images and PyAV for videos.
"""
from typing import Optional
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import asyncio

from app.backend.models.media import MediaFile
from app.backend.config import ThumbnailSettings
from app.backend.utils.logging_config import get_api_logger

_logger = get_api_logger()


class ThumbnailGenerator:
    """Generate thumbnails for images and videos"""

    def __init__(self, settings: ThumbnailSettings):
        self.settings = settings
        self._executor = ThreadPoolExecutor(max_workers=settings.max_concurrent)

    async def generate_thumbnail(
        self,
        media_file: MediaFile,
        size: str = "medium"
    ) -> Optional[str]:
        """
        Main entry point. Detects media type and calls appropriate generator.
        Returns thumbnail_path on success, None on failure (sets error).

        Args:
            media_file: MediaFile model instance
            size: Thumbnail size ('small', 'medium', 'large')

        Returns:
            Path to generated thumbnail on success, None on failure
        """
        if not self.settings.enabled:
            _logger.info(f"Thumbnail generation is disabled")
            return None

        # Validate media type
        if media_file.media_type.lower() not in ['image', 'video']:
            _logger.info(f"Skipping thumbnail for media type: {media_file.media_type}")
            return None

        # Get source file path
        source_path = Path(media_file.file_path)
        if not source_path.exists():
            _logger.error(f"Source file not found: {source_path}")
            return None

        # Get thumbnail dimensions
        size_map = {
            'small': self.settings.small_size,
            'medium': self.settings.medium_size,
            'large': self.settings.large_size
        }
        dimensions = size_map.get(size, self.settings.medium_size)

        # Generate output path
        output_path = self._get_thumbnail_path(media_file, size)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            # Generate thumbnail based on media type
            if media_file.media_type.lower() == 'image':
                success = await asyncio.get_event_loop().run_in_executor(
                    self._executor,
                    self._generate_image_thumbnail,
                    source_path,
                    output_path,
                    dimensions
                )
            elif media_file.media_type.lower() == 'video':
                success = await asyncio.get_event_loop().run_in_executor(
                    self._executor,
                    self._generate_video_thumbnail,
                    source_path,
                    output_path,
                    dimensions
                )
            else:
                return None

            if success:
                _logger.info(f"Thumbnail generated: {output_path}")
                return str(output_path)
            else:
                _logger.error(f"Thumbnail generation failed for {media_file.id}")
                return None

        except Exception as e:
            _logger.error(f"Thumbnail generation error for {media_file.id}: {e}")
            return None

    def _generate_image_thumbnail(
        self,
        source_path: Path,
        output_path: Path,
        size: tuple[int, int]
    ) -> bool:
        """
        Use PIL to resize image. Run in thread pool.

        Args:
            source_path: Path to source image
            output_path: Path to save thumbnail
            size: Tuple of (width, height)

        Returns:
            True on success, False on failure
        """
        try:
            from PIL import Image

            # Open and process image
            with Image.open(source_path) as img:
                # Convert to RGB if needed (handles RGBA, P, etc.)
                if img.mode not in ('RGB', 'L'):
                    img = img.convert('RGB')

                # Create thumbnail (maintains aspect ratio)
                img.thumbnail(size, Image.Resampling.LANCZOS)

                # Save with quality setting
                img.save(
                    output_path,
                    format=self.settings.format,
                    quality=self.settings.quality,
                    optimize=True
                )

            return True

        except Exception as e:
            _logger.error(f"Image thumbnail generation failed: {e}")
            return False

    def _generate_video_thumbnail(
        self,
        source_path: Path,
        output_path: Path,
        size: tuple[int, int]
    ) -> bool:
        """
        Use PyAV to extract frame, PIL to resize. Run in thread pool.

        Args:
            source_path: Path to source video
            output_path: Path to save thumbnail
            size: Tuple of (width, height)

        Returns:
            True on success, False on failure
        """
        try:
            import av
            from PIL import Image

            # Open video container
            with av.open(str(source_path)) as container:
                # Get video stream
                video_stream = container.streams.video[0]

                if video_stream is None:
                    _logger.error(f"No video stream found in {source_path}")
                    return False

                # Calculate target position
                duration = video_stream.duration
                if duration and duration > 0:
                    # Convert duration to seconds
                    time_base = video_stream.time_base
                    duration_seconds = duration * float(time_base)
                    target_seconds = duration_seconds * self.settings.video_frame_position

                    # Seek to position
                    target_pts = int(target_seconds / float(time_base))
                    container.seek(target_pts, stream=video_stream)

                # Decode first frame after seek
                for frame in container.decode(video=0):
                    # Convert frame to PIL Image
                    img = frame.to_image()

                    # Convert to RGB if needed
                    if img.mode not in ('RGB', 'L'):
                        img = img.convert('RGB')

                    # Create thumbnail
                    img.thumbnail(size, Image.Resampling.LANCZOS)

                    # Save thumbnail
                    img.save(
                        output_path,
                        format=self.settings.format,
                        quality=self.settings.quality,
                        optimize=True
                    )

                    # Only need first frame
                    break
                else:
                    _logger.error(f"No frames found in video {source_path}")
                    return False

            return True

        except Exception as e:
            _logger.error(f"Video thumbnail generation failed: {e}")
            return False

    def _get_thumbnail_path(self, media_file: MediaFile, size: str) -> Path:
        """
        Generate organized thumbnail path: same dir as original with _thumb_{size} suffix

        Args:
            media_file: MediaFile model instance
            size: Thumbnail size identifier

        Returns:
            Path object for thumbnail location
        """
        source_path = Path(media_file.file_path)
        parent_dir = source_path.parent

        # Generate thumbnail filename
        stem = source_path.stem
        thumb_filename = f"{stem}_thumb_{size}.jpg"

        return parent_dir / thumb_filename

    def __del__(self):
        """Cleanup executor on deletion"""
        if hasattr(self, '_executor'):
            self._executor.shutdown(wait=False)