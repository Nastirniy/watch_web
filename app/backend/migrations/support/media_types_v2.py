"""The names revision 036_webm_media_types rewrites. FROZEN once 036 has shipped: never import enums/media_types.py or
services/files/formats.py here (they move on; this map must not). tests/test_migration_036_webm_media_types.py checks
it against the defaults of 036.

A row of a file with an extension of WEBM_EXTENSIONS (lower case, with the dot) holding OLD_NAME is a WebM file: libmagic
called WebM video/x-matroska, the container WebM is a profile of. 032 left those rows alone (media_types_v1.EXTENSIONS:
the target of video/x-matroska there is the Matroska format, which owns only .mkv).
"""
from typing import Tuple

OLD_NAME = "video/x-matroska"
WEBM = "video/webm"
WEBM_EXTENSIONS: Tuple[str, ...] = (".webm",)
