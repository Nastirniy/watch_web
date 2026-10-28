"""The one content sniffer of every upload: libmagic on the head of the file.

detect_content_type names the bytes (never the extension or the declared Content-Type: bytes libmagic cannot name
are application/octet-stream, which no format accepts). The registry's content types and aliases follow what
Debian's libmagic (the api image) reports. It reads only the first SNIFF_BYTES and blocks: run it in a thread from
async code.
"""
from __future__ import annotations

from pathlib import Path

import magic

from app.backend.enums.media_types import ContentType
from app.backend.services.files.formats import normalise_content_type

SNIFF_BYTES = 8192
UNKNOWN_CONTENT_TYPE = ContentType.OCTET_STREAM


def detect_content_type(path: Path) -> str:
    """libmagic's MIME type of the first SNIFF_BYTES, lower case, parameters cut; application/octet-stream when
    libmagic names nothing"""
    with open(path, "rb") as source:
        head = source.read(SNIFF_BYTES)
    return normalise_content_type(magic.from_buffer(head, mime=True)) or UNKNOWN_CONTENT_TYPE.value
