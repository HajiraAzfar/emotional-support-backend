"""
PHQ-9 and GAD-7, once a week inside the evening check-in.

The answers are the evening schedule's phq9_* and gad7_* values, worded exactly
as published (capture_fallbacks.json). When the check-in completes, each
questionnaire is stored as one questionnaire_responses row, like the earlier
wellbeing questionnaire: completed with its score, or declined when it was left
part-way — either way the next one is due a week later.

As with that questionnaire (FR-INS-018/019), the score is never shown to the
user: Insights draws an unlabelled line. Severity bands exist for the clinician
report only, which is not built yet.

PHQ-9 item 9 asks about thoughts of being better off dead or of self-harm.
Any answer above "Not at all" is a safety flag, handled by the entries router
the moment it is recorded (crisis content, the resources screen, a crisis event).
"""
import json
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models.questionnaire_response import QuestionnaireResponse

PHQ9, GAD7 = "phq9", "gad7"
INSTRUMENTS = {
    PHQ9: {"version": "phq9-v1", "items": [f"phq9_{i}" for i in range(1, 10)], "max": 27},
    GAD7: {"version": "gad7-v1", "items": [f"gad7_{i}" for i in range(1, 8)], "max": 21},
}
EVERY = timedelta(days=7)
# The item that is a safety question in its own right.
SAFETY_ITEM = "phq9_9"
COMPLETED, DECLINED = "completed", "declined"

# Published cut-offs (Kroenke 2001; Spitzer 2006): (lowest total, band).
_BANDS = {
    PHQ9: [(0, "minimal"), (5, "mild"), (10, "moderate"), (15, "moderately severe"), (20, "severe")],
    GAD7: [(0, "minimal"), (5, "mild"), (10, "moderate"), (15, "severe")],
}


def severity_band(instrument: str, total: int) -> str:
    """The published band for a total. For the clinician report; never shown to the user."""
    if not 0 <= total <= INSTRUMENTS[instrument]["max"]:
        raise ValueError(f"{instrument} total out of range: {total}")
    band = _BANDS[instrument][0][1]
    for lowest, name in _BANDS[instrument]:
        if total >= lowest:
            band = name
    return band


def due(db: Session, account_id, before: datetime) -> set[str]:
    """
    The questionnaires due for an entry started at `before`: none answered or
    declined in the week before it. Judged at the entry's start, so finishing
    one inside this entry never makes the rest of it disappear mid-way.
    """
    out = set()
    for name, spec in INSTRUMENTS.items():
        last = (
            db.query(QuestionnaireResponse.created_at)
            .filter(QuestionnaireResponse.account_id == account_id,
                    QuestionnaireResponse.version == spec["version"],
                    QuestionnaireResponse.created_at < before)
            .order_by(QuestionnaireResponse.created_at.desc())
            .first()
        )
        if last is None or last.created_at <= before - EVERY:
            out.add(name)
    return out


def record_from(db: Session, account_id, recorded: dict, asked: set[str]) -> list[QuestionnaireResponse]:
    """
    Store each questionnaire this entry asked: completed with its score when
    every item was answered, declined otherwise. `asked` is the instruments
    that were due for it.
    """
    rows = []
    for name in sorted(asked):
        spec = INSTRUMENTS[name]
        answers = {item: recorded.get(item) for item in spec["items"]}
        complete = all(isinstance(v, int) for v in answers.values())
        row = QuestionnaireResponse(
            account_id=account_id,
            outcome=COMPLETED if complete else DECLINED,
            version=spec["version"],
            score=sum(answers.values()) if complete else None,
            max_score=spec["max"] if complete else None,
            answers=json.dumps({k: v for k, v in answers.items() if v is not None})[:500],
        )
        db.add(row)
        rows.append(row)
    return rows


def trend(db: Session, account_id, instrument: str, start: datetime | None, end: datetime | None) -> dict:
    """FR-INS-019: positions between the lowest and highest score, no numbers, no bands."""
    query = db.query(QuestionnaireResponse).filter(
        QuestionnaireResponse.account_id == account_id,
        QuestionnaireResponse.outcome == COMPLETED,
        QuestionnaireResponse.version == INSTRUMENTS[instrument]["version"],
    )
    if start is not None:
        query = query.filter(QuestionnaireResponse.created_at >= start)
    if end is not None:
        query = query.filter(QuestionnaireResponse.created_at <= end)
    points = [
        {"date": row.created_at.date().isoformat(), "position": round(row.score / row.max_score, 3)}
        for row in query.order_by(QuestionnaireResponse.created_at).all()
        if row.max_score
    ]
    return {"points": points, "needed": max(0, 2 - len(points))}
