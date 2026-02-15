from pydantic import BaseModel


class ListAttachedMediaRequest(BaseModel):
    observation_id: int
