"""Helpers shared by the media routes: lookups, access checks and file responses."""
import re
from pathlib import Path
from typing import Iterator, Optional, Tuple

from fastapi import Response
from fastapi.responses import StreamingResponse
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.models.media import MediaFile
from app.backend.utils.content_type import served_content_type
from app.backend.utils.row_locks import lock_first
from app.backend.models.observation import Observation
from app.backend.config import settings
from app.backend.errors.problems import ApiError
from app.backend.enums.errors import ErrorCode


def storage_root() -> Path:
    root = Path(settings.storage.upload_path)
    return root if root.is_absolute() else root.resolve()


async def get_observation_from_media(session: AsyncSession, media_file: MediaFile) -> Observation:
    """Observation the media belongs to; 404 when it is missing or deleted"""
    obs_query = select(Observation).where(
        Observation.id == media_file.observation_id,
        Observation.is_deleted == False
    )
    result = await session.exec(obs_query)
    observation = result.first()

    if not observation:
        raise ApiError(ErrorCode.MEDIA_OBSERVATION_NOT_FOUND)

    return observation


async def get_media_or_404(session: AsyncSession, media_id: int, include_deleted: bool = False,
                           lock: bool = False) -> MediaFile:
    """The media file; lock=True locks it as the first statement of the write (utils/row_locks.py)"""
    query = select(MediaFile).where(MediaFile.id == media_id)
    if not include_deleted:
        query = query.where(MediaFile.is_deleted == False)
    media_file = await lock_first(session, query) if lock else (await session.exec(query)).first()
    if not media_file:
        raise ApiError(ErrorCode.MEDIA_NOT_FOUND)
    return media_file


def accel_redirect_path(relative_path: str) -> str:
    """nginx internal path for a file under the uploads root: {nginx_internal_uploads_prefix}/{relative}"""
    prefix = settings.storage.nginx_internal_uploads_prefix.rstrip("/")
    return f"{prefix}/{relative_path.replace(chr(92), '/').lstrip('/')}"


_SINGLE_RANGE = re.compile(r"^bytes\s*=\s*(\d*)\s*-\s*(\d*)$", re.IGNORECASE)
_CHUNK = 256 * 1024


def parse_byte_range(header: Optional[str], size: int) -> Optional[Tuple[int, int]]:
    """Inclusive (first, last) byte of the one range a Range header asks for (RFC 9110, section 14).

    None: serve the whole file with 200, because there is no header, its unit is not bytes, the syntax is invalid
    (also a last byte before the first) or it lists several ranges (a multipart answer is not offered, which the
    RFC allows). Unsatisfiable (first byte past the end, an empty suffix, an empty file): ApiError 416.
    """
    if not header:
        return None
    match = _SINGLE_RANGE.match(header.strip())
    if not match or (not match.group(1) and not match.group(2)):
        return None
    first, last = match.group(1), match.group(2)
    unsatisfiable = ApiError(
        ErrorCode.RANGE_NOT_SATISFIABLE, headers={"Content-Range": f"bytes */{size}"}, size_bytes=size,
    )
    if not first:  # suffix: the last N bytes
        count = int(last)
        if count == 0 or size == 0:
            raise unsatisfiable
        return max(0, size - count), size - 1
    start = int(first)
    if last and int(last) < start:
        return None
    if start >= size:
        raise unsatisfiable
    return start, min(int(last), size - 1) if last else size - 1


def _read_range(path: Path, start: int, length: int) -> Iterator[bytes]:
    """Chunks of a file part; the file is never read whole into memory"""
    with open(path, "rb") as handle:
        handle.seek(start)
        while length > 0:
            chunk = handle.read(min(_CHUNK, length))
            if not chunk:
                return
            length -= len(chunk)
            yield chunk


def original_file_response(media_file: MediaFile, disposition: str, range_header: Optional[str] = None) -> Response:
    """Serve the original file: X-Accel-Redirect behind nginx (which handles Range), otherwise the API streams it
    and answers a single byte range itself (parse_byte_range)"""
    filename = media_file.original_filename.replace('"', "")
    headers = {
        # the stored canonical name (text types with their charset); in the headers on both paths: Starlette would
        # add its own charset to a media_type= text type
        "Content-Type": served_content_type(media_file.mime_type),
        "Content-Disposition": f'{disposition}; filename="{filename}"',
        "Cache-Control": "private, max-age=3600",
        "ETag": f'"{media_file.file_hash}"',
        "X-File-Hash": media_file.file_hash,
    }
    # The API decides whether the file is there, also before handing it to nginx: otherwise nginx answered its
    # own 404 page instead of media_file_missing (tests/test_media_download.py)
    root = storage_root()
    path = media_file.resolve_path(root)
    if not path.is_file():
        raise ApiError(ErrorCode.MEDIA_FILE_MISSING)

    if settings.storage.enable_nginx:
        try:
            relative = path.resolve().relative_to(root).as_posix()
        except ValueError:
            raise ApiError(ErrorCode.MEDIA_PATH_UNRESOLVED)
        headers.update({
            "X-Accel-Redirect": accel_redirect_path(relative),
            "Accept-Ranges": "bytes",
            "X-Accel-Buffering": "yes",
        })
        return Response(content=b"", status_code=200, headers=headers)

    size = path.stat().st_size
    byte_range = parse_byte_range(range_header, size)
    headers["Accept-Ranges"] = "bytes"
    if byte_range is None:
        start, length, status_code = 0, size, 200
    else:
        start, last = byte_range
        length, status_code = last - start + 1, 206
        headers["Content-Range"] = f"bytes {start}-{last}/{size}"
    headers["Content-Length"] = str(length)
    return StreamingResponse(
        _read_range(path, start, length), status_code=status_code, headers=headers,
    )


def source_file_exists(media_file: MediaFile) -> bool:
    """The file imgproxy renders the thumbnail from (the image or the poster) is on disk"""
    source = media_file.thumbnail_source_path
    return bool(source) and (storage_root() / source).is_file()
