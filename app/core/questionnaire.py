"""
The wellbeing questionnaire (SRS 4.10, FR-INS-017/018/019).

Three rules shape everything here:
  - It is offered again only after a set number of days from the last offer,
    whether she completed it or declined (FR-INS-017).
  - The score is calculated and stored, and never shown to her (FR-INS-018).
  - The trend is a line with no numbers, no severity bands and no condition
    named (FR-INS-019) — so the API returns a position between 0 and 1 rather
    than the score itself, and the client has no number it could display.

The instrument itself is a generic placeholder: content/questionnaire.json says
so, and it must be replaced with one the Clinical Advisor has chosen.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.models.questionnaire_response import QuestionnaireResponse

_CONTENT = Path(__file__).resolve().parent.parent / "content" / "questionnaire.json"

with open(_CONTENT, encoding="utf-8") as f:
    FORM: dict = json.load(f)

VERSION: str = FORM["version"]
ITEMS: list[dict] = FORM["items"]
SCALE: list[dict] = FORM["scale"]
OFFER_EVERY_DAYS: int = FORM["offer_every_days"]
MAX_SCORE: int = len(ITEMS) * max(option["value"] for option in SCALE)
# A line needs two points; one dot is not a trend.
MIN_POINTS = 2

COMPLETED, DECLINED = "completed", "declined"


class InvalidAnswers(ValueError):
    pass


def spec() -> dict:
    """What the client needs to show the form. No scoring information at all."""
    return {
        "version": VERSION,
        "title": FORM["title"],
        "intro": FORM["intro"],
        "scale": SCALE,
        "items": ITEMS,
    }


def _last_offer(db: Session, account_id) -> QuestionnaireResponse | None:
    return (
        db.query(QuestionnaireResponse)
        .filter(QuestionnaireResponse.account_id == account_id)
        .order_by(QuestionnaireResponse.created_at.desc())
        .first()
    )


def status(db: Session, account_id, now: datetime | None = None) -> dict:
    """Whether to offer it, and when the next offer falls due (FR-INS-017)."""
    now = now or datetime.utcnow()
    last = _last_offer(db, account_id)
    if last is None:
        return {"due": True, "next_due": None}
    next_due = last.created_at + timedelta(days=OFFER_EVERY_DAYS)
    return {"due": now >= next_due, "next_due": next_due.date().isoformat()}


def score_for(answers: dict[str, Any]) -> int:
    """Raises InvalidAnswers unless every item has an answer from the scale."""
    allowed = {option["value"] for option in SCALE}
    total = 0
    for item in ITEMS:
        value = answers.get(item["id"])
        if isinstance(value, bool) or not isinstance(value, int) or value not in allowed:
            raise InvalidAnswers(f"{item['id']} must be one of {sorted(allowed)}")
        total += value
    unknown = set(answers) - {item["id"] for item in ITEMS}
    if unknown:
        raise InvalidAnswers(f"Unknown items: {sorted(unknown)}")
    return total


def record(db: Session, account_id, answers: dict[str, Any]) -> QuestionnaireResponse:
    """FR-INS-018: the score is stored here and returned to no one."""
    total = score_for(answers)
    response = QuestionnaireResponse(
        account_id=account_id,
        outcome=COMPLETED,
        version=VERSION,
        score=total,
        max_score=MAX_SCORE,
        answers=json.dumps(answers, ensure_ascii=False)[:500],
    )
    db.add(response)
    db.commit()
    return response


def decline(db: Session, account_id) -> QuestionnaireResponse:
    """A decline delays the next offer exactly as a completion does."""
    response = QuestionnaireResponse(account_id=account_id, outcome=DECLINED, version=VERSION)
    db.add(response)
    db.commit()
    return response


def trend(db: Session, account_id, start: datetime | None, end: datetime | None) -> dict:
    """
    FR-INS-019: an unlabelled line. Each point is where the score sat between
    the lowest and highest possible answer — a position, not a number — so the
    client has nothing it could print beside the dot.

    Only responses to the current version are plotted: comparing scores from
    two different instruments would draw a change that never happened.
    """
    query = (
        db.query(QuestionnaireResponse)
        .filter(
            QuestionnaireResponse.account_id == account_id,
            QuestionnaireResponse.outcome == COMPLETED,
            QuestionnaireResponse.version == VERSION,
        )
    )
    if start is not None:
        query = query.filter(QuestionnaireResponse.created_at >= start)
    if end is not None:
        query = query.filter(QuestionnaireResponse.created_at <= end)

    points = [
        {
            "date": response.created_at.date().isoformat(),
            "position": round(response.score / response.max_score, 3) if response.max_score else 0,
        }
        for response in query.order_by(QuestionnaireResponse.created_at).all()
    ]
    return {"points": points, "needed": max(0, MIN_POINTS - len(points))}
