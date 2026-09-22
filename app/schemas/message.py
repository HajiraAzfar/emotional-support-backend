import uuid
from datetime import datetime
from pydantic import BaseModel, Field


class MessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=5000)


class MessageOut(BaseModel):
    id: uuid.UUID
    entry_id: uuid.UUID
    role: str
    kind: str | None = None
    value_id: str | None = None
    content: str
    sequence: int
    created_at: datetime

    class Config:
        from_attributes = True
