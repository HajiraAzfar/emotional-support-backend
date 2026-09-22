import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel
from app.schemas.message import MessageOut


class EntryCreate(BaseModel):
    journal_type: str


class ScaleOption(BaseModel):
    value: int
    label: str


class CaptureSpec(BaseModel):
    """The value currently being requested, and the control to render for it (FR-ENT-025)."""
    value_id: str
    control: Literal["scale", "multi_select", "free_text"]
    required: bool
    library: str | None = None
    scale: list[ScaleOption] | None = None
    max_length: int | None = None
    # Library categories of this valence are listed first (e.g. positive feelings when savouring).
    prefer_valence: str | None = None


class CaptureAnswer(BaseModel):
    value_id: str
    # int for a scale, list of library ids for multi_select, text for free_text.
    value: int | list[str] | str | None = None
    skipped: bool = False


class ConversationChoice(BaseModel):
    # continue / stop answer the offer (FR-AIR-001); end is the "End session" control (FR-AIR-005).
    choice: Literal["continue", "stop", "end"]


class EntryOut(BaseModel):
    id: uuid.UUID
    journal_type: str
    domains: list[str] | None = None
    status: str
    started_at: datetime
    completed_at: datetime | None = None
    # None until capture finishes, then: offered | declined | active | closed | suppressed
    conversation_status: str | None = None
    # Highest crisis tier in this entry: None/clear | mild | danger | emergency
    crisis_tier: str | None = None
    # FR-CRIS-005: resource reference shown at the end of a completed mild-tier entry.
    support_note: str | None = None
    next_capture: CaptureSpec | None = None
    messages: list[MessageOut] = []

    class Config:
        from_attributes = True


class CrisisEventOut(BaseModel):
    tier: Literal["mild", "danger", "emergency"]
    variant: str | None = None
    # Fixed content (FR-CRIS-008). Emergency content stays on screen until acknowledged (FR-CRIS-007).
    text: str | None = None


class EntryStepOut(EntryOut):
    """Returned by every action inside an entry, so the client can re-render the whole thread."""
    crisis_event: CrisisEventOut | None = None  # raised by this action, if any
    referral: bool = False  # gently suggest professional support


class EntrySummary(BaseModel):
    id: uuid.UUID
    journal_type: str
    status: str
    started_at: datetime
    completed_at: datetime | None = None
    last_activity_at: datetime
    mood: int | None = None
