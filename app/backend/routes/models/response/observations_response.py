from pydantic import BaseModel, Field


class MessageResponse(BaseModel):  # TODO: i think we need make it global accessible to other routes and use it
    """Generic message response"""

    message: str = Field(description="Response message")
