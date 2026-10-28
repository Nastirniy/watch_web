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
    """A binary body in one of media_types (members of enums/media_types.ContentType)"""
    return {status: {
        "description": description,
        "content": {str(media_type): {"schema": BINARY} for media_type in media_types},
        **({"headers": headers} if headers else {}),
    }}


def empty_response(status: int, description: str, headers: Optional[Dict[str, dict]] = None) -> Dict[int, dict]:
    """No body; headers carry the result"""
    return {status: {"description": description, **({"headers": headers} if headers else {})}}


def with_headers(status: int, description: str, headers: Dict[str, dict]) -> Dict[int, dict]:
    """Headers of a response whose body is the route's response_model"""
    return {status: {"description": description, "headers": headers}}
