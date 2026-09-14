import uuid
from datetime import datetime
from pydantic import BaseModel
from app.schemas.message import MessageOut


class EntryCreate(BaseModel):
    journal_type: str


class EntryOut(BaseModel):
    id: uuid.UUID
    journal_type: str
    domains: list[str] | None = None
    status: str
    current_step: int | None = None
    started_at: datetime
    completed_at: datetime | None = None
    messages: list[MessageOut] = []

    class Config:
        from_attributes = True