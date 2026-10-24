"""
Insights (SRS 4.10): counts and series from this account's completed entries.

Mostly read-only. The weekly report writes in two narrow places: it awards the
badges she has become eligible for, and it saves the line she chooses to carry
into next week. Both are hers, both are deterministic, and neither is anything
the model decided.
"""
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core import badges, checkin_pdf, checkin_reports, insights, narrative, pdf_report, questionnaire, weekly
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

@router.get("/report.pdf")
def get_report_pdf(
    period: str = Query(insights.DEFAULT_PERIOD),
    tz_offset: int = Query(0, ge=TZ_OFFSET_MIN, le=TZ_OFFSET_MAX),
    db: Session = Depends(get_db),
    account: Account = Depends(get_current_user),
):
    """Phase B: the same period's insights, as a PDF to keep or share."""
    if period not in insights.PERIODS:
        raise HTTPException(status_code=422, detail=f"Period must be one of: {', '.join(insights.PERIODS)}")
    data = insights.build(db, account.id, period, _terms(db, account), tz_offset=tz_offset,
                          weekly_goal=account.weekly_goal)
    today = insights.local_date(datetime.utcnow(), tz_offset)
    pdf = pdf_report.render(data, name=account.preferred_name, made_on=today)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="mind-doc-report-{today.isoformat()}.pdf"',
            # The user's entries: never kept by a cache on the way.
            "Cache-Control": "no-store",
        },
    )


@router.get("/check-ins/reports")
def get_checkin_reports(
    tz_offset: int = Query(0, ge=TZ_OFFSET_MIN, le=TZ_OFFSET_MAX),
    db: Session = Depends(get_db),
    account: Account = Depends(get_current_user),
):
    """The check-in reports she can open: finished days, weeks and months, newest first."""
    return checkin_reports.listing(db, account.id, tz_offset)


@router.get("/check-ins/report.pdf")
def get_checkin_report_pdf(
    kind: str = Query(...),
    start: date = Query(...),
    tz_offset: int = Query(0, ge=TZ_OFFSET_MIN, le=TZ_OFFSET_MAX),
    db: Session = Depends(get_db),
    account: Account = Depends(get_current_user),
):
    """One day's, week's or month's check-ins as a PDF, once that period is over on her clock."""
    if kind not in checkin_reports.KINDS:
        raise HTTPException(status_code=422, detail=f"Kind must be one of: {', '.join(checkin_reports.KINDS)}")
    try:
        report = checkin_reports.build(db, account.id, kind, start, _terms(db, account), tz_offset)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    except checkin_reports.NotReady:
        raise HTTPException(status_code=409,
                            detail=f"This report is ready once the {checkin_reports.NOUNS[kind]} is over.")
    except checkin_reports.Empty:
        raise HTTPException(status_code=404, detail="No check-ins were finished in this period.")
    today = insights.local_date(datetime.utcnow(), tz_offset)
    pdf = checkin_pdf.render(report, name=account.preferred_name, made_on=today)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="mind-doc-{kind}-{report["start"]}.pdf"',
            "Cache-Control": "no-store",
        },
    )


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


# --- Your Week in Reflection ------------------------------------------------

# Long enough for a sentence she means, short enough to stay a single thought.
REFLECTION_MAX = 300


class WeeklyReflectionIn(BaseModel):
    week_start: date
    # Empty clears it: she can take back what she wrote.
    text: str = Field(default="", max_length=REFLECTION_MAX)


def _terms(db: Session, account: Account) -> dict:
    return {
        term.item_id: term.name
        for term in db.query(UserTerm).filter(UserTerm.account_id == account.id).all()
    }


def _monday(week_start: date | None, tz_offset: int) -> date:
    """Any day in a week opens that week; no argument means this one."""
    if week_start is None:
        return weekly.current_week_start(tz_offset)
    return weekly.week_start_for(week_start)


@router.get("/weekly")
def get_weekly(
    week_start: date | None = Query(None),
    tz_offset: int = Query(0, ge=TZ_OFFSET_MIN, le=TZ_OFFSET_MAX),
    narrative_text: bool = Query(True, alias="narrative"),
    db: Session = Depends(get_db),
    account: Account = Depends(get_current_user),
):
    """
    One week, counted from her entries. The narrative is added on top and is
    allowed to fail: `narrative` comes back null and the rest still stands.
    """
    report = weekly.build(db, account.id, _monday(week_start, tz_offset), _terms(db, account),
                          tz_offset=tz_offset)
    report["narrative"] = narrative.write(report) if narrative_text else None
    return report


@router.get("/weekly/history")
def get_weekly_history(
    tz_offset: int = Query(0, ge=TZ_OFFSET_MIN, le=TZ_OFFSET_MAX),
    db: Session = Depends(get_db),
    account: Account = Depends(get_current_user),
):
    """The weeks she can open, newest first."""
    return {"weeks": weekly.history(db, account.id, tz_offset=tz_offset)}


@router.post("/weekly/reflection")
def save_weekly_reflection(
    payload: WeeklyReflectionIn,
    tz_offset: int = Query(0, ge=TZ_OFFSET_MIN, le=TZ_OFFSET_MAX),
    db: Session = Depends(get_db),
    account: Account = Depends(get_current_user),
):
    """"Carry into next week" — saved only because she chose to, and editable."""
    week = weekly.week_start_for(payload.week_start)
    return {"week_start": week.isoformat(),
            "text": weekly.save_reflection(db, account.id, week, payload.text)}


@router.get("/badges")
def get_badges(
    db: Session = Depends(get_db),
    account: Account = Depends(get_current_user),
):
    """Every badge with its rule, earned first. Locked ones show what earns them."""
    badges.evaluate(db, account.id)
    return {"badges": badges.all_for(db, account.id)}
