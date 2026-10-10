import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field
from app.schemas.message import MessageOut

class EntryCreate(BaseModel):
    journal_type: str
    # FR-JRN-006: start a further exposure cycle from a completed one.
    parent_entry_id: uuid.UUID | None = None


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
    # The user may send this value in several messages before moving on.
    repeatable: bool = False
    # Library categories of this valence are listed first (e.g. positive feelings when savouring).
    prefer_valence: str | None = None
    # She may speak this answer instead of typing it.
    voice: bool = False


class CaptureAnswer(BaseModel):
    value_id: str
    # int for a scale, list of library ids for multi_select, text for free_text.
    value: int | list[str] | str | None = None
    skipped: bool = False
    # Repeatable values only: she is still writing, so keep this value open
    # and do not move on yet.
    more: bool = False
    # Made by the app for this answer: a retry after a lost response is recorded once.
    client_id: uuid.UUID | None = None


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
    # FR-JRN-007: shown before the first value; the client must acknowledge it.
    pending_notice: str | None = None
    # FR-JRN-006: the entry is waiting for her to go and do the thing she planned.
    # No value is requested until she says she is back.
    pending_resume: bool = False
    parent_entry_id: uuid.UUID | None = None
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


class CheckInCreate(BaseModel):
    """The whole daily check-in, sent once at the end. Empty factors or note are simply empty."""
    # Made by the app, so a retry after a lost response is saved once.
    client_id: uuid.UUID
    mood: int = Field(ge=1, le=5)
    factors: list[str] = Field(default_factory=list, max_length=60)
    note: str = Field(default="", max_length=2000)


class CheckInOut(BaseModel):
    entry_id: uuid.UUID
    # Echo's closing reply, and the helplines card drawn under it (soft | prominent), if any.
    closing: str
    card: Literal["soft", "prominent"] | None = None
    crisis_event: CrisisEventOut | None = None
    # Several low-mood check-ins in a row: the app gently points to Chat.
    suggest: Literal["chat"] | None = None


class EntrySummary(BaseModel):
    id: uuid.UUID
    journal_type: str
    status: str
    started_at: datetime
    completed_at: datetime | None = None
    last_activity_at: datetime
    mood: int | None = None
    # The user's name for a deep-dive entry, or a date-based one (FR-ENT-009).
    name: str | None = None
    # Which exposure cycle this is: 1 for the first, 2 for the next, and so on (FR-JRN-006).
    cycle: int | None = None
    # The opening of what she wrote, so an entry is recognisable in the list.
    preview: str | None = None
    # An AI Chat whose conversation is "active" can be carried on from the list.
    conversation_status: str | None = None
