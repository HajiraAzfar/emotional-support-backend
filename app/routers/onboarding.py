import json
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.account import Account
from app.models.focus_area import FocusArea
from app.schemas.onboarding import (
    ConsentRequest,
    DistressBaselineRequest,
    FocusAreasRequest,
    GoalRequest,
    OnboardingStatusResponse,
)

router = APIRouter(prefix="/onboarding", tags=["onboarding"])

ALLOWED_GOALS = {2, 3, 5, 7}

_CONTENT_DIR = Path(__file__).resolve().parent.parent / "content"

with open(_CONTENT_DIR / "distress_scale.json", encoding="utf-8") as f:
    DISTRESS_SCALE = json.load(f)

with open(_CONTENT_DIR / "focus_areas.json", encoding="utf-8") as f:
    FOCUS_AREAS = json.load(f)


@router.get("/status", response_model=OnboardingStatusResponse)
def status_(
    account: Account = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    codes = [f.code for f in db.query(FocusArea).filter(FocusArea.account_id == account.id).all()]
    return {
        "consent_version": account.consent_version,
        "consent_at": account.consent_at,
        "focus_areas": codes,
        "distress_baseline": account.distress_baseline,
        "weekly_goal": account.weekly_goal,
        "current_consent_version": settings.CONSENT_VERSION,
    }


@router.get("/distress-scale")
def distress_scale():
    return DISTRESS_SCALE


@router.get("/focus-areas/options")
def focus_area_options():
    return FOCUS_AREAS


@router.post("/consent", response_model=OnboardingStatusResponse)
def consent(
    payload: ConsentRequest,
    account: Account = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    account.consent_version = payload.version
    account.consent_at = datetime.utcnow()
    db.commit()
    db.refresh(account)
    return status_(account=account, db=db)


@router.put("/focus-areas", response_model=OnboardingStatusResponse)
def focus_areas(
    payload: FocusAreasRequest,
    account: Account = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    unknown = set(payload.codes) - set(FOCUS_AREAS)
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unknown focus area codes: {', '.join(sorted(unknown))}",
        )

    db.query(FocusArea).filter(FocusArea.account_id == account.id).delete()

    for code in set(payload.codes):
        db.add(FocusArea(account_id=account.id, code=code))

    db.commit()
    return status_(account=account, db=db)


@router.post("/distress-baseline", response_model=OnboardingStatusResponse)
def distress_baseline(
    payload: DistressBaselineRequest,
    account: Account = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    account.distress_baseline = payload.value
    account.distress_baseline_at = datetime.utcnow()
    db.commit()
    db.refresh(account)
    return status_(account=account, db=db)


@router.put("/goal", response_model=OnboardingStatusResponse)
def goal(
    payload: GoalRequest,
    account: Account = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    if payload.weekly_goal not in ALLOWED_GOALS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Weekly goal must be 2, 3, 5, or 7.",
        )

    account.weekly_goal = payload.weekly_goal
    db.commit()
    db.refresh(account)
    return status_(account=account, db=db)