"""
Crisis handling around the detection rules in crisis_check.py.

- assess(): two-stage tier — lexical rules and the model tier, higher wins (FR-CRIS-002)
- record(): stores a content-free detection record (FR-CRIS-013), raises the
  entry's tier, and picks which fixed content to show (FR-CRIS-008, FR-CRIS-012)
- suppressed(): danger/emergency stop every AI call for that entry (FR-CRIS-006/007)
"""
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.crisis_check import assess_lexical, higher_tier
from app.models.crisis_event import CrisisEvent
from app.models.entry import Entry

_CONTENT_PATH = Path(__file__).resolve().parent.parent / "content" / "crisis_responses.json"
with open(_CONTENT_PATH, encoding="utf-8") as f:
    CONTENT: dict = json.load(f)

SUPPRESSING_TIERS = {"danger", "emergency"}
REPEAT_WINDOW = timedelta(days=7)


@dataclass
class CrisisResult:
    tier: str
    variant: str | None  # full | abbreviated | None for mild
    text: str | None  # fixed content to show in the thread; None for mild

    def as_dict(self) -> dict:
        return {"tier": self.tier, "variant": self.variant, "text": self.text}


def assess(text: str, model_tier: str | None) -> str:
    """Lexical tier and model tier, higher wins. Model unavailable → lexical alone."""
    return higher_tier(assess_lexical(text), model_tier or "clear")


def suppressed(entry: Entry | None) -> bool:
    return bool(entry and entry.crisis_tier in SUPPRESSING_TIERS)


def _danger_variant(db: Session, account_id) -> str:
    """FR-CRIS-012: full the first time, abbreviated within 7 days of the last presentation."""
    recent = db.query(CrisisEvent).filter(
        CrisisEvent.account_id == account_id,
        CrisisEvent.tier == "danger",
        CrisisEvent.variant.isnot(None),
        CrisisEvent.created_at >= datetime.utcnow() - REPEAT_WINDOW,
    ).first()
    return "abbreviated" if recent else "full"


def record(db: Session, account_id, entry: Entry | None, field: str, tier: str) -> CrisisResult | None:
    """Returns None for clear. Never stores the evaluated text."""
    if tier == "clear":
        return None

    if tier == "mild":
        variant = None
    elif tier == "danger":
        variant = _danger_variant(db, account_id)
    else:
        variant = "full"

    db.add(CrisisEvent(
        account_id=account_id,
        entry_id=entry.id if entry else None,
        field=field,
        tier=tier,
        variant=variant,
    ))
    if entry is not None:
        entry.crisis_tier = higher_tier(entry.crisis_tier or "clear", tier)
    db.flush()

    text = CONTENT[tier][variant] if variant else None
    return CrisisResult(tier=tier, variant=variant, text=text)


def mild_reference() -> str:
    return CONTENT["mild"]["reference"]
