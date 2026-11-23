"""File names: the one extension parser and the one sanitiser (no other module parses an extension by hand)."""
from __future__ import annotations

import re
import unicodedata
import uuid
from typing import Optional

MAX_FILENAME_LENGTH = 255

# Refused whatever a format lists (every upload: media and user guides)
DANGEROUS_EXTENSIONS = frozenset({
    '.exe', '.bat', '.cmd', '.com', '.pif', '.scr', '.vbs', '.js', '.jar',
    '.app', '.deb', '.pkg', '.dmg', '.iso', '.msi', '.run', '.rpm'
})


def extension_of(filename: Optional[str]) -> str:
    """Lower-case extension with the dot; "" when the name has none"""
    if not filename or "." not in filename:
        return ""
    ext = "." + filename.rsplit(".", 1)[1].lower()
    return ext if len(ext) > 1 else ""


def header_filename(value: Optional[str]) -> Optional[str]:
    """A file name from a request header (X-Original-Filename). The ASGI server hands header bytes over as Latin-1
    text; a client sends a name as its UTF-8 bytes, so they are read again as UTF-8 (strict: bytes that are no UTF-8,
    the encoding of a lone surrogate included, stay Latin-1). The name then goes through sanitize_filename as a
    multipart file name does, which Starlette also reads as UTF-8 with a Latin-1 fallback."""
    if not value:
        return value
    try:
        return value.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        return value


def sanitize_filename(filename: str) -> str:
    """The one file-name sanitiser: cross-platform safe, ASCII stem, extension kept"""
    if not filename:
        return f"upload_{uuid.uuid4().hex[:8]}"

    # The extension comes from the original name by the one parser (extension_of, as check_declared_format reads
    # it: ".jpg" has the extension .jpg), its case kept; only the stem is reduced to ASCII, so a stem without ASCII
    # letters cannot swallow the extension
    if extension_of(filename):
        name = filename[:filename.rfind(".")]
        ext = filename[len(name):]
    else:
        name, ext = filename, ""
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
