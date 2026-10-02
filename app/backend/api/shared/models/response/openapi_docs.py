"""OpenAPI descriptions of success responses that are not a JSON model: files, images, empty bodies, headers.

Use in a route decorator:  @router.get(..., response_class=Response, responses=file_response(...))
"""
from typing import Dict, Iterable, Optional

BINARY = {"type": "string", "format": "binary"}


def header(description: str, schema_type: str = "string", fmt: Optional[str] = None) -> Dict[str, dict]:
    schema = {"type": schema_type}
    if fmt:
        schema["format"] = fmt
    return {"description": description, "schema": schema}


def file_response(description: str, media_types: Iterable[str], headers: Optional[Dict[str, dict]] = None,
                  status: int = 200) -> Dict[int, dict]:
    """A binary body in one of media_types"""
    return {status: {
        "description": description,
        "content": {media_type: {"schema": BINARY} for media_type in media_types},
        **({"headers": headers} if headers else {}),
    }}


def empty_response(status: int, description: str, headers: Optional[Dict[str, dict]] = None) -> Dict[int, dict]:
    """No body; headers carry the result"""
    return {status: {"description": description, **({"headers": headers} if headers else {})}}


def with_headers(status: int, description: str, headers: Dict[str, dict]) -> Dict[int, dict]:
    """Headers of a response whose body is the route's response_model"""
    return {status: {"description": description, "headers": headers}}


# Headers shared by the file and upload routes
ORIGINAL_FILE_HEADERS = {
    "Content-Disposition": header('attachment (download) or inline (stream), with filename="<original name>"'),
    "ETag": header("sha256 of the file, quoted"),
    "X-File-Hash": header("sha256 of the file"),
    "Accept-Ranges": header("bytes: the file can be read in parts with Range"),
    "Cache-Control": header("private, max-age=3600"),
}
UPLOAD_SESSION_HEADERS = {
    "Upload-Offset": header("Bytes received so far: send the next chunk from here", "integer"),
    "Upload-Length": header("expected_size of the session", "integer"),
    "Upload-Expires": header("When the session expires", "string", "date-time"),
    "Upload-Status": header("open, completed, aborted or expired"),
    "Cache-Control": header("no-store"),
}
THUMBNAIL_PENDING_HEADERS = {
    "Retry-After": header("Seconds to wait before asking again", "integer"),
    "X-Thumbnail-Status": header("pending or processing"),
    "Cache-Control": header("no-store"),
}
