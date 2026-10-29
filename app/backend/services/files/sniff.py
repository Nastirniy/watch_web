"""The one content sniffer of every upload: libmagic on the head of the file, and the text rule of text/* formats.

detect_content_type names the bytes (never the extension or the declared Content-Type: bytes libmagic cannot name
are application/octet-stream, which no format accepts). The registry's content types and aliases follow what
Debian's libmagic (the api image) reports. check_text holds a text/* format to UTF-8 (a BOM allowed) without NUL
bytes, over the whole file: the sniffer only sees its first SNIFF_BYTES. Both read the file in chunks, never whole;
both block: run them in a thread from async code.
"""
from __future__ import annotations

import codecs
from pathlib import Path

import magic

from app.backend.enums.errors import ErrorCode
from app.backend.enums.general import MediaType
from app.backend.enums.media_types import ContentType
from app.backend.services.files.formats import UnsupportedMediaFormat, normalise_content_type
from app.backend.utils.content_type import parse_content_type

SNIFF_BYTES = 8192
TEXT_TOP_LEVEL = "text"
TEXT_CHUNK_BYTES = 1024 * 1024
UNKNOWN_CONTENT_TYPE = ContentType.OCTET_STREAM


def detect_content_type(path: Path) -> str:
    """libmagic's MIME type of the first SNIFF_BYTES, lower case, parameters cut; application/octet-stream when
    libmagic names nothing"""
    with open(path, "rb") as source:
        head = source.read(SNIFF_BYTES)
    return normalise_content_type(magic.from_buffer(head, mime=True)) or UNKNOWN_CONTENT_TYPE.value


def is_text_format(content_type: str) -> bool:
    """A format held to the text rule: its content type is text/*"""
    parsed = parse_content_type(content_type)
    return parsed is not None and parsed.type == TEXT_TOP_LEVEL


def check_text(path: Path, content_type: str, chunk_size: int = TEXT_CHUNK_BYTES) -> None:
    """A text/* file is stored only as UTF-8 (a leading BOM allowed) without a NUL byte: else 415
    document_text_invalid. content_type is the format's (the param of the problem); neither the bytes nor the
    decoder's message ever reach it."""
    decoder = codecs.getincrementaldecoder("utf-8-sig")()
    try:
        with open(path, "rb") as source:
            while chunk := source.read(chunk_size):
                if b"\x00" in chunk:
                    raise _invalid_text(content_type)
                decoder.decode(chunk)
        decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        raise _invalid_text(content_type) from None


def _invalid_text(content_type: str) -> UnsupportedMediaFormat:
    return UnsupportedMediaFormat(ErrorCode.DOCUMENT_TEXT_INVALID, MediaType.DOCUMENT.value, content_type=content_type)
