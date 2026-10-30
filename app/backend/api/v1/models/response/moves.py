from typing import List, Optional

from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.backend.api.shared.helpers.move_helper import retry_after
from app.backend.api.shared.openapi_docs import header
from app.backend.api.versioning import PublishedVersion
from app.backend.enums.general import MoveNodeKind, MoveStatus
from app.backend.enums.routes import MoveRoutes
from app.backend.services.moves import MoveView
from app.backend.utils.validators import ResponseDatetime


class MoveUploadResponse(BaseModel):
    """An upload the move waits for"""
    upload_id: str
    post_id: int
    received_bytes: int
    total_bytes: int
    last_chunk_at: Optional[ResponseDatetime] = Field(description="When bytes last arrived; null: none yet")
    holding: bool = Field(description="false: no bytes for too long, or past its expiry: it no longer keeps the move waiting and is left alone")


class MoveResponse(BaseModel):
    """A move that waits, or waited, for the uploads below its node"""
    id: int
    status: MoveStatus
    node_kind: MoveNodeKind
    node_id: int
    target_group_id: Optional[int] = Field(description="The group it goes to; null: directly under the root, or hidden (target_visible false)")
    target_visible: bool = Field(description="false: it goes to a group outside the reader's scope_group.view; the user who asked always sees the target")
    requested_by: int
    requested_at: ResponseDatetime
    finished_at: Optional[ResponseDatetime]
    force: bool = Field(description="true: run at once over running uploads, which went on")
    error: Optional[str] = Field(description="The problem code that refused it when it ran (status failed)")
    waiting_seconds: int = Field(description="Seconds it has waited, or waited in all once it ended")
    uploads: List[MoveUploadResponse] = Field(description="Uploads below the node while it waits; empty once it ended")


# What a move route answers when uploads hold the move
MOVE_ACCEPTED = {202: {
    "model": MoveResponse,
    "description": "Uploads run below the node: nothing moved yet, the move waits, or this repeats the move that waits. Follow it at Location",
    "headers": {
        "Location": header("URL of the move operation"),
        "Retry-After": header("Seconds before it is worth asking again", "integer"),
    },
}}


def move_response(view: MoveView) -> MoveResponse:
    return MoveResponse.model_validate(view, from_attributes=True)


def move_accepted(view: MoveView, version: PublishedVersion) -> JSONResponse:
    """The 202 of a move that waits; the URL points to the API version the client called"""
    return JSONResponse(
        status_code=202, content=move_response(view).model_dump(mode="json"),
        headers={"Location": version.path(MoveRoutes.MOVE_BY_ID, move_id=view.id), **retry_after()},
    )
