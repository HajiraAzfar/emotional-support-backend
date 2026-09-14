import uuid
from datetime import datetime
from pydantic import BaseModel


class MessageCreate(BaseModel):
    content: str


class MessageOut(BaseModel):
    id: uuid.UUID
    entry_id: uuid.UUID
    role: str
    content: str
    sequence: int
    created_at: datetime

    class Config:
        from_attributes = True