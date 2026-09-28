import uuid
from datetime import datetime
from pydantic import BaseModel, Field


class MessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=5000)
    # She has more to say before Echo answers: the message is stored and screened
    # for crisis, but no reply is generated until she sends one without this.
    more: bool = False


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
