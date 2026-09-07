from datetime import datetime

from pydantic import BaseModel, Field


class ConsentRequest(BaseModel):
    version: str = Field(max_length=20)


class FocusAreasRequest(BaseModel):
    codes: list[str]


class DistressBaselineRequest(BaseModel):
    value: int = Field(ge=0, le=10)


class GoalRequest(BaseModel):
    weekly_goal: int = Field(ge=2, le=7)


class OnboardingStatusResponse(BaseModel):
    consent_version: str | None
    consent_at: datetime | None
    focus_areas: list[str]
    distress_baseline: int | None
    weekly_goal: int
    current_consent_version: str