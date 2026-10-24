"""What the app sends for a Thought Journal record (core/thought.py checks the rest)."""
import uuid
from typing import Literal

from pydantic import BaseModel, Field

TEXT = 2000


class EmotionIn(BaseModel):
    feeling: str = Field(min_length=1, max_length=60)
    # How strong, 0-100: at the time, and again after the work. Either may be missing.
    before: int | None = Field(None, ge=0, le=100)
    after: int | None = Field(None, ge=0, le=100)


class ThoughtContent(BaseModel):
    """Everything a record can hold; a draft may hold any part of it."""
    situation: str | None = Field(None, max_length=TEXT)
    situation_tags: list[str] = Field(default_factory=list, max_length=7)
    emotions: list[EmotionIn] = Field(default_factory=list, max_length=12)
    automatic_thought: str | None = Field(None, max_length=TEXT)
    second_thought: str | None = Field(None, max_length=TEXT)
    # How much she believes the main thought, 0-100%: before, and after the work.
    belief_before: int | None = Field(None, ge=0, le=100)
    belief_after: int | None = Field(None, ge=0, le=100)
    thinking_traps: list[str] = Field(default_factory=list, max_length=15)
    evidence_for: str | None = Field(None, max_length=TEXT)
    evidence_against: str | None = Field(None, max_length=TEXT)
    balanced_thought: str | None = Field(None, max_length=TEXT)
    balanced_belief: int | None = Field(None, ge=0, le=100)
    next_step: str | None = Field(None, max_length=500)


class ThoughtSave(ThoughtContent):
    mode: Literal["quick", "full"] = "quick"
    # True finishes the record; a draft is saved with False.
    complete: bool = False
    tz_offset: int = Field(0, ge=-840, le=840)
    timezone: str | None = Field(None, max_length=64)
    # The check-in it was opened from (its feelings come ticked).
    source_entry_id: uuid.UUID | None = None


class FavoriteIn(BaseModel):
    is_favorite: bool


class QuestionIn(BaseModel):
    side: Literal["for", "against"]
