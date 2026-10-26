from datetime import datetime

from pydantic import BaseModel, Field


class ConsentRequest(BaseModel):
    version: str = Field(max_length=20)


class FocusAreasRequest(BaseModel):
    codes: list[str]


class WorkIssuesRequest(BaseModel):
    codes: list[str]


class LifeVisionRequest(BaseModel):
    text: str = Field(max_length=160)


class DistressBaselineRequest(BaseModel):
    value: int = Field(ge=0, le=10)


class NameRequest(BaseModel):
    # Empty or missing clears it: the greeting goes back to having no name.
    name: str | None = Field(default=None, max_length=40)


class OnboardingStatusResponse(BaseModel):
    consent_version: str | None
    consent_at: datetime | None
    focus_areas: list[str]
    work_issues: list[str] | None = None
    life_vision: str | None = None
    distress_baseline: int | None
    current_consent_version: str
    elevated_distress: bool = False
    preferred_name: str | None = None