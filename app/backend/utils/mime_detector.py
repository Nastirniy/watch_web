"""
Advanced MIME type detection utility with fallbacks and validation.
"""
import magic
import mimetypes
from typing import Optional, Tuple
import logging

_logger = logging.getLogger(__name__)


class MimeTypeDetector:
    """Advanced MIME type detector with multiple detection methods and validation."""
    
    # Extended MIME type mapping for common file extensions
    EXTENSION_MIME_MAP = {
        # Images
        'jpg': 'image/jpeg', 'jpeg': 'image/jpeg', 'png': 'image/png',
        'gif': 'image/gif', 'bmp': 'image/bmp', 'webp': 'image/webp',
        'svg': 'image/svg+xml', 'ico': 'image/x-icon', 'tiff': 'image/tiff',
        'tif': 'image/tiff', 'heic': 'image/heic', 'heif': 'image/heif',
        
        # Videos
        'mp4': 'video/mp4', 'avi': 'video/x-msvideo', 'mov': 'video/quicktime',
        'wmv': 'video/x-ms-wmv', 'flv': 'video/x-flv', 'webm': 'video/webm',
        'mkv': 'video/x-matroska', '3gp': 'video/3gpp', 'ogv': 'video/ogg',
        'm4v': 'video/x-m4v', 'asf': 'video/x-ms-asf',
        
        # Audio
        'mp3': 'audio/mpeg', 'wav': 'audio/wav', 'ogg': 'audio/ogg',
        'flac': 'audio/flac', 'aac': 'audio/aac', 'wma': 'audio/x-ms-wma',
        'm4a': 'audio/mp4', 'opus': 'audio/opus', 'aiff': 'audio/aiff',
        
        # Documents
        'pdf': 'application/pdf', 'doc': 'application/msword',
        'docx': 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
        'xls': 'application/vnd.ms-excel',
        'xlsx': 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        'ppt': 'application/vnd.ms-powerpoint',
        'pptx': 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
        'odt': 'application/vnd.oasis.opendocument.text',
        'ods': 'application/vnd.oasis.opendocument.spreadsheet',
        'odp': 'application/vnd.oasis.opendocument.presentation',
        
        # Text
        'txt': 'text/plain', 'rtf': 'application/rtf', 'csv': 'text/csv',
        'xml': 'application/xml', 'html': 'text/html', 'htm': 'text/html',
        'json': 'application/json', 'yaml': 'application/x-yaml',
        'yml': 'application/x-yaml',
        
        # Archives
        'zip': 'application/zip', 'rar': 'application/vnd.rar',
        '7z': 'application/x-7z-compressed', 'tar': 'application/x-tar',
        'gz': 'application/gzip', 'bz2': 'application/x-bzip2',
        'xz': 'application/x-xz',
        
        # Executables and binaries
        'exe': 'application/x-msdownload', 'msi': 'application/x-msi',
        'deb': 'application/vnd.debian.binary-package',
        'rpm': 'application/x-rpm', 'dmg': 'application/x-apple-diskimage',
        
        # Other common types
        'iso': 'application/x-iso9660-image', 'torrent': 'application/x-bittorrent',
        'swf': 'application/x-shockwave-flash',
    }
    
    # MIME types that should be treated as safe/allowed
    ALLOWED_MIME_PATTERNS = [
        'image/', 'video/', 'audio/', 'text/', 'application/pdf',
        'application/zip', 'application/json', 'application/xml',
        'application/vnd.ms-', 'application/vnd.openxmlformats-',
        'application/vnd.oasis.opendocument.'
    ]
    
    def __init__(self, use_magic: bool = True, strict_validation: bool = False):
        """
        Initialize the MIME type detector.
        
        Args:
            use_magic: Whether to use python-magic for detection
            strict_validation: Whether to enforce strict MIME type validation
        """
        self.use_magic = use_magic
        self.strict_validation = strict_validation
        self._magic_mime = None
        
        if self.use_magic:
            try:
                self._magic_mime = magic.Magic(mime=True)
                _logger.info("python-magic initialized successfully")
            except Exception as e:
                _logger.warning(f"Failed to initialize python-magic: {e}")
                self.use_magic = False
    
    def detect_mime_type(self, file_data: bytes, filename: Optional[str] = None) -> Tuple[str, str]:
        """
        Detect MIME type using multiple methods with fallbacks.
        
        Args:
            file_data: The file data bytes
            filename: Optional filename for extension-based detection
            
        Returns:
            Tuple of (mime_type, detection_method)
        """
        mime_type = None
        method = "unknown"
        
        # Method 1: Magic number detection (most reliable)
        if self.use_magic and self._magic_mime and len(file_data) > 0:
            try:
                mime_type = self._magic_mime.from_buffer(file_data)
                if mime_type and mime_type != 'application/octet-stream':
                    method = "magic"
                    _logger.debug(f"Magic detection: {mime_type}")
                else:
                    mime_type = None
            except Exception as e:
                _logger.warning(f"Magic detection failed: {e}")
        
        # Method 2: Python's mimetypes module with filename
        if not mime_type and filename:
            try:
                detected_type, _ = mimetypes.guess_type(filename)
                if detected_type:
                    mime_type = detected_type
                    method = "mimetypes"
                    _logger.debug(f"Mimetypes detection: {mime_type}")
            except Exception as e:
                _logger.warning(f"Mimetypes detection failed: {e}")
        
        # Method 3: Extension-based detection (fallback)
        if not mime_type and filename:
            extension = self._extract_extension(filename)
            if extension in self.EXTENSION_MIME_MAP:
                mime_type = self.EXTENSION_MIME_MAP[extension]
                method = "extension"
                _logger.debug(f"Extension detection: {mime_type}")
        
        # Method 4: Content-based heuristics for common formats
        if not mime_type:
            mime_type = self._detect_by_content(file_data)
            if mime_type:
                method = "content_heuristic"
                _logger.debug(f"Content heuristic detection: {mime_type}")
        
        # Final fallback
        if not mime_type:
            mime_type = "application/octet-stream"
            method = "fallback"
        
        # Validate MIME type if strict validation is enabled
        if self.strict_validation and not self._is_allowed_mime_type(mime_type):
            _logger.warning(f"MIME type {mime_type} not in allowed list")
            mime_type = "application/octet-stream"
            method = "restricted"
        
        return mime_type, method
    
    def _extract_extension(self, filename: str) -> str:
        """Extract file extension in lowercase."""
        if not filename or '.' not in filename:
            return ""
        return filename.split('.')[-1].lower()
    
    def _detect_by_content(self, file_data: bytes) -> Optional[str]:
        """Detect MIME type by examining file content signatures."""
        if len(file_data) < 8:
            return None
        
        # Common file signatures
        signatures = {
            # Images
            b'\xFF\xD8\xFF': 'image/jpeg',
            b'\x89PNG\r\n\x1a\n': 'image/png',
            b'GIF87a': 'image/gif',
            b'GIF89a': 'image/gif',
            b'RIFF': self._check_riff_format,  # Could be WebP or AVI
            b'BM': 'image/bmp',
            
            # Videos
            b'\x00\x00\x00\x18ftypmp4': 'video/mp4',
            b'\x00\x00\x00 ftypM4V': 'video/x-m4v',
            
            # Audio
            b'ID3': 'audio/mpeg',  # MP3 with ID3 tag
            b'\xFF\xFB': 'audio/mpeg',  # MP3 without ID3
            b'RIFF': self._check_riff_format,  # Could be WAV
            b'OggS': 'audio/ogg',
            b'fLaC': 'audio/flac',
            
            # Documents
            b'%PDF': 'application/pdf',
            b'PK\x03\x04': self._check_zip_format,  # ZIP-based formats
            b'\xD0\xCF\x11\xE0': 'application/vnd.ms-office',  # MS Office old format
            
            # Archives
            b'Rar!\x1a\x07\x00': 'application/vnd.rar',
            b'7z\xbc\xaf\x27\x1c': 'application/x-7z-compressed',
        }
        
        for signature, mime_type_or_func in signatures.items():
            if file_data.startswith(signature):
                if callable(mime_type_or_func):
                    return mime_type_or_func(file_data)
                return mime_type_or_func
        
        return None
    
    def _check_riff_format(self, file_data: bytes) -> Optional[str]:
        """Check RIFF format subtype (WebP, WAV, AVI, etc.)."""
        if len(file_data) < 12:
            return None
        
        if file_data[8:12] == b'WEBP':
            return 'image/webp'
        elif file_data[8:12] == b'WAVE':
            return 'audio/wav'
        elif file_data[8:12] == b'AVI ':
            return 'video/x-msvideo'
        
        return 'application/octet-stream'
    
    def _check_zip_format(self, file_data: bytes) -> str:
        """Check ZIP-based format (DOCX, XLSX, etc.)."""
        # This is a simplified check - in practice, you'd need to
        # examine the ZIP contents to determine the exact format
        return 'application/zip'
    
    def _is_allowed_mime_type(self, mime_type: str) -> bool:
        """Check if MIME type is in the allowed list."""
        return any(mime_type.startswith(pattern) for pattern in self.ALLOWED_MIME_PATTERNS)
    
    def get_media_type_category(self, mime_type: str) -> str:
        """Get the general media category from MIME type."""
        if mime_type.startswith('image/'):
            return 'image'
        elif mime_type.startswith('video/'):
            return 'video'
        elif mime_type.startswith('audio/'):
            return 'audio'
        elif mime_type.startswith('text/'):
            return 'document'
        elif mime_type in ['application/pdf', 'application/msword',
                          'application/vnd.openxmlformats-officedocument.wordprocessingml.document']:
            return 'document'
        elif mime_type.startswith('application/zip') or mime_type.startswith('application/x-'):
            return 'archive'
        else:
            return 'other'


# Global instance
mime_detector = MimeTypeDetector(use_magic=True, strict_validation=False)