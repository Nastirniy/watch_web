"""Helpers shared by the media routes: lookups, access checks and file responses."""
from pathlib import Path

from fastapi import Response
from fastapi.responses import FileResponse
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.models.media import MediaFile
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


async def get_media_or_404(session: AsyncSession, media_id: int, include_deleted: bool = False) -> MediaFile:
    query = select(MediaFile).where(MediaFile.id == media_id)
    if not include_deleted:
        query = query.where(MediaFile.is_deleted == False)
    media_file = (await session.exec(query)).first()
    if not media_file:
        raise ApiError(ErrorCode.MEDIA_NOT_FOUND)
    return media_file


def accel_redirect_path(relative_path: str) -> str:
    """nginx internal path for a file under the uploads root: {nginx_internal_uploads_prefix}/{relative}"""
    prefix = settings.storage.nginx_internal_uploads_prefix.rstrip("/")
    return f"{prefix}/{relative_path.replace(chr(92), '/').lstrip('/')}"


def original_file_response(media_file: MediaFile, disposition: str) -> Response:
    """Serve the original file: X-Accel-Redirect behind nginx, FileResponse (Range, sendfile) otherwise"""
    filename = media_file.original_filename.replace('"', "")
    headers = {
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
            "Content-Type": media_file.mime_type,
            "Accept-Ranges": "bytes",
            "X-Accel-Buffering": "yes",
        })
        return Response(content=b"", status_code=200, headers=headers)
    return FileResponse(path=str(path), media_type=media_file.mime_type, headers=headers)
