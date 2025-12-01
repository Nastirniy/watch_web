from pydantic import BaseModel
from typing import List


class ChunkedUploadStart(BaseModel):
    observation_id: int
    original_filename: str
    file_size: int
    chunk_size: int
    total_chunks: int
    file_extension: str
    media_type: str


class ResumeUploadRequest(BaseModel):
    observation_id: int
    original_filename: str
    file_size: int
    file_hash: str
    media_type: str
