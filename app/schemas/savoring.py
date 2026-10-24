"""What the app sends for a Saving Journal moment (core/savoring.py checks the rest)."""
import uuid
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field

from app.core import savoring


class SavoringContent(BaseModel):
    """Everything she can put down about a moment. Only what happened is needed."""
    type: Literal["moment", "reminisce", "anticipate"] = "moment"
    what_happened: str = Field(min_length=1, max_length=savoring.LIMITS[savoring.V_WHAT])
    categories: list[str] = Field(default_factory=list, max_length=savoring.CATEGORIES_MAX)
    people: list[str] = Field(default_factory=list, max_length=savoring.TAGS_MAX)
    places: list[str] = Field(default_factory=list, max_length=savoring.TAGS_MAX)
    mood_before: int | None = Field(None, ge=0, le=10)
    # Feeling id → how strong, 0-10; None when she named it without a number. In her order.
    emotions: dict[str, int | None] = Field(default_factory=dict, max_length=savoring.EMOTIONS_MAX)
    sensory_note: str | None = Field(None, max_length=savoring.LIMITS[savoring.V_SENSORY])
    mood_after: int | None = Field(None, ge=0, le=10)
    techniques: list[str] = Field(default_factory=list, max_length=10)
    why_good: str | None = Field(None, max_length=savoring.LIMITS[savoring.V_WHY])
    souvenir_line: str | None = Field(None, max_length=savoring.LIMITS[savoring.V_SOUVENIR])
    # A moment she looks forward to: how good she expects it to be, and when.
    expected_rating: int | None = Field(None, ge=0, le=10)
    event_date: date | None = None


class SavoringCreate(SavoringContent):
    # Made by the app, so a retry (or an offline moment synced twice) is saved once.
    client_id: uuid.UUID | None = None
    mode: Literal["quick", "full"] = "quick"
    # The day it belongs to on her clock: today, or yesterday. None means today.
    local_date: date | None = None
    tz_offset: int = Field(0, ge=-840, le=840)
    timezone: str | None = Field(None, max_length=64)
    # The check-in it was opened from (its positive feelings come ticked).
    source_entry_id: uuid.UUID | None = None


class FavoriteIn(BaseModel):
    is_favorite: bool


class ActualIn(BaseModel):
    actual_rating: int = Field(ge=0, le=10)
