"""What the app sends for the Exposure Journal: ladders, and exposure records (core/exposure.py checks the rest)."""
import uuid
from typing import Literal

from pydantic import BaseModel, Field

TEXT = 2000
STEP_TEXT = 300


class StepIn(BaseModel):
    description: str = Field(min_length=1, max_length=STEP_TEXT)
    expected_suds: int = Field(ge=0, le=100)


class LadderCreate(BaseModel):
    fear_name: str = Field(min_length=1, max_length=200)
    tags: list[str] = Field(default_factory=list, max_length=6)
    steps: list[StepIn] = Field(default_factory=list, max_length=10)


class LadderUpdate(BaseModel):
    fear_name: str | None = Field(None, min_length=1, max_length=200)
    tags: list[str] | None = Field(None, max_length=6)
    archived: bool | None = None


class StepUpdate(BaseModel):
    description: str | None = Field(None, min_length=1, max_length=STEP_TEXT)
    expected_suds: int | None = Field(None, ge=0, le=100)


class Reorder(BaseModel):
    item_ids: list[uuid.UUID] = Field(max_length=10)


class Reading(BaseModel):
    minute: int = Field(ge=0, le=600)
    suds: int = Field(ge=0, le=100)


class ExposureContent(BaseModel):
    """Everything an exposure can hold; a draft may hold any part of it."""
    ladder_item_id: uuid.UUID | None = None
    feared_outcome: str | None = Field(None, max_length=TEXT)
    predicted_likelihood: int | None = Field(None, ge=0, le=100)
    predicted_severity: int | None = Field(None, ge=0, le=100)
    predicted_anxiety: int | None = Field(None, ge=0, le=100)
    safety_behaviors_planned: list[str] = Field(default_factory=list, max_length=6)
    learning_goal: str | None = Field(None, max_length=500)
    # Her local time, "YYYY-MM-DDTHH:MM"; the reminder itself lives on the phone.
    scheduled_for: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")
    feelings: list[str] = Field(default_factory=list, max_length=12)
    anxiety_peak: int | None = Field(None, ge=0, le=100)
    anxiety_end: int | None = Field(None, ge=0, le=100)
    anxiety_readings: list[Reading] = Field(default_factory=list, max_length=60)
    what_happened: str | None = Field(None, max_length=TEXT)
    outcome_occurred: Literal["yes", "partly", "no"] | None = None
    safety_behaviors_used: list[str] = Field(default_factory=list, max_length=6)
    learning: str | None = Field(None, max_length=TEXT)
    next_step: Literal["repeat", "move_up", "variation"] | None = None


class ExposureSave(ExposureContent):
    mode: Literal["quick", "full"] = "full"
    # draft: the plan is being written; planned: ready (and maybe scheduled);
    # completed / partial / skipped: how it went.
    status: Literal["draft", "planned", "completed", "partial", "skipped"] = "draft"
    tz_offset: int = Field(0, ge=-840, le=840)
    timezone: str | None = Field(None, max_length=64)
    source_entry_id: uuid.UUID | None = None


class FavoriteIn(BaseModel):
    is_favorite: bool
