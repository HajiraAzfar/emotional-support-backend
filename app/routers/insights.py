"""
Insights (SRS 4.10): counts and series from this account's completed entries.
Read-only — this router never writes anything.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core import insights, questionnaire
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.account import Account
from app.models.user_term import UserTerm

router = APIRouter(prefix="/insights", tags=["insights"])


# Minutes east of UTC, as the device reports them (Pakistan is +300). Anything
# beyond real-world offsets is a client bug, not a timezone.
TZ_OFFSET_MIN, TZ_OFFSET_MAX = -840, 840


@router.get("")
def get_insights(
    period: str = Query(insights.DEFAULT_PERIOD),
    tz_offset: int = Query(0, ge=TZ_OFFSET_MIN, le=TZ_OFFSET_MAX),
    db: Session = Depends(get_db),
    account: Account = Depends(get_current_user),
):
    """FR-INS-001: one period applies to every view returned here."""
    if period not in insights.PERIODS:
        raise HTTPException(status_code=422, detail=f"Period must be one of: {', '.join(insights.PERIODS)}")

    # Her own words are labelled like the predefined ones (FR-PICK-006).
    terms = {
        term.item_id: term.name
        for term in db.query(UserTerm).filter(UserTerm.account_id == account.id).all()
    }
    # Days are hers, not the server's: an entry at 1am is that day's entry.
    return insights.build(db, account.id, period, terms, tz_offset=tz_offset, weekly_goal=account.weekly_goal)

class QuestionnaireAnswers(BaseModel):
    # item id → value from the scale; validated against the instrument itself.
    answers: dict[str, int]


@router.get("/questionnaire")
def get_questionnaire(account: Account = Depends(get_current_user)):
    """
    FR-INS-017: the questions themselves. Nothing here says what is being
    measured or how it is scored — she is not shown a score at any point.
    """
    return questionnaire.spec()


@router.post("/questionnaire", status_code=status.HTTP_201_CREATED)
def submit_questionnaire(
    payload: QuestionnaireAnswers,
    db: Session = Depends(get_db),
    account: Account = Depends(get_current_user),
):
    """FR-INS-018: the score is stored and deliberately not returned."""
    try:
        questionnaire.record(db, account.id, payload.answers)
    except questionnaire.InvalidAnswers as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"recorded": True}


@router.post("/questionnaire/decline", status_code=status.HTTP_201_CREATED)
def decline_questionnaire(
    db: Session = Depends(get_db),
    account: Account = Depends(get_current_user),
):
    """FR-INS-017: declining is a first-class answer; the next offer waits the same period."""
    questionnaire.decline(db, account.id)
    return {"recorded": True}
