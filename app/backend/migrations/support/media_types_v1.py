"""The canonical names of stored media types at revision 032_canonical_media_types. FROZEN once 032 has shipped: 032
rewrites old rows with it. Never import enums/media_types.py or services/files/formats.py here (they move on; this
map must not). tests/migrations/test_migration_032_canonical_media_types.py checks it against the defaults of 032.

ALIASES: an alias of exactly one default format at 032 (no default format's own name) -> that format's canonical
name, on a row whose file extension is one of EXTENSIONS of that name. BY_EXTENSION: an alias of several default formats (text/plain: Markdown and CSV; also the plain-text format's
own name) -> the canonical name by the row's file extension (lower case, with the dot); another extension keeps the
value. SEEDED_RENAMES: the default formats whose canonical name changed in 032 (old seeded content_type -> new): a
media_formats row still holding the old name is renamed, the old name becoming its first alias. SEEDED_ALIASES: the
seeded alias lists the defaults trimmed, replaced only while a row still holds the list as seeded.
"""
from typing import Dict, List, Tuple

ALIASES: Dict[str, str] = {
    "image/pjpeg": "image/jpeg",
    "video/x-m4v": "video/mp4",
    "video/x-matroska": "video/matroska",
    "audio/mp3": "audio/mpeg",
    "audio/x-wav": "audio/wav",
    "audio/wave": "audio/wav",
    "audio/vnd.wave": "audio/wav",
    "audio/x-flac": "audio/flac",
    "audio/opus": "audio/ogg",
    "application/ogg": "audio/ogg",
    "audio/x-m4a": "audio/mp4",
    "audio/aac": "audio/mp4",
    "text/rtf": "application/rtf",
}

# The extensions of each canonical name of ALIASES at 032 (lower case, with the dot): an alias is rewritten only on a
# row whose extension belongs to that format (application/ogg on a .ogv file is not audio/ogg, video/x-matroska on a
# .webm file is not this Matroska format); any other row keeps its value
EXTENSIONS: Dict[str, Tuple[str, ...]] = {
    "image/jpeg": (".jpg", ".jpeg"),
    "video/mp4": (".mp4", ".m4v"),
    "video/matroska": (".mkv",),
    "audio/mpeg": (".mp3",),
    "audio/wav": (".wav",),
    "audio/flac": (".flac",),
    "audio/ogg": (".ogg", ".oga", ".opus"),
    "audio/mp4": (".m4a",),
    "application/rtf": (".rtf",),
}

BY_EXTENSION: Dict[str, Dict[str, str]] = {
    "text/plain": {".md": "text/markdown", ".markdown": "text/markdown", ".csv": "text/csv"},
}

SEEDED_RENAMES: Dict[str, str] = {
    "video/x-matroska": "video/matroska",
}

_WORDML = "application/vnd.openxmlformats-officedocument."

# The document formats seeded by the first commit of the file-format registry (unreleased) listed aliases Debian's
# libmagic never reports for those files; the defaults dropped them. content_type -> (aliases as seeded then, aliases
# now): a row whose aliases are still exactly the old list gets the new one, an edited list stays.
SEEDED_ALIASES: Dict[str, Tuple[List[str], List[str]]] = {
    _WORDML + "wordprocessingml.document": (["application/zip"], []),
    _WORDML + "spreadsheetml.sheet": (["application/zip"], []),
    _WORDML + "presentationml.presentation": (["application/zip"], []),
    "application/vnd.oasis.opendocument.text": (["application/zip"], []),
    "application/msword": (["application/x-ole-storage", "application/CDFV2"], []),
    "text/csv": (["text/plain", "application/csv"], ["text/plain"]),
}
