"""
Crisis handling around the detection rules in crisis_check.py.

- assess(): two-stage tier — lexical rules and the model tier, higher wins (FR-CRIS-002)
- record(): stores a content-free detection record (FR-CRIS-013), raises the
  entry's tier, and picks which fixed content to show (FR-CRIS-008, FR-CRIS-012)
- suppressed(): danger/emergency stop every AI call for a journal entry (FR-CRIS-006/007);
  AI Chat keeps going
- triage() / card_for(): the always-on safety check run before the skill is chosen
"""
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from app.core import language
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


# ---------- journals only (AI Chat never calls these; it has triage() below) ----------
# Draft rules, pending the clinical advisor like the rest of crisis_check.py.

# Common misspellings of the words the rules look for, so "kil myslef" is read
# as "kill myself". Journals only: AI Chat keeps the rules exactly as they were.
_TYPOS = {
    "myslef": "myself", "mysef": "myself", "my self": "myself", "kil": "kill", "wnat": "want",
    "sucide": "suicide", "suicde": "suicide", "suiside": "suicide", "sucidal": "suicidal",
    "suicdal": "suicidal", "suisidal": "suicidal", "khudkhushi": "khudkushi", "khudkashi": "khudkushi",
}
_TYPO_RE = re.compile(r"\b(" + "|".join(re.escape(k) for k in sorted(_TYPOS, key=len, reverse=True)) + r")\b", re.I)
# Clearly in the past, or about someone else...
_PAST = re.compile(
    r"\b(years? ago|months? ago|(a )?long time ago|in the past|used to|back then|"
    r"when i was (young|little|a (kid|child|teen|teenager)|in (school|college|university))|"
    r"as a (kid|child|teen|teenager)|(saal|mahine|mahinay|baras) pehle|bohot pehle|bachpan)\b", re.I)
_OTHER = re.compile(
    r"\b(my|meri|mera|mere|meray) (friend|dost|sister|behen|bhai|brother|cousin|mother|mom|ammi|"
    r"father|dad|abbu|colleague|classmate|neighbou?r|aunt|uncle|khala|phuppo|saheli)\b", re.I)
# ...unless anything says it is (or may be) happening now: then it stays danger.
_PRESENT = re.compile(
    r"\b(now|still|again|today|tonight|anymore|any more|these days|lately|recently|this week|"
    r"abhi|ab bhi|ab tak|phir se|dobara|aaj|aajkal|aaj kal|i want|i wanna|i feel like|"
    r"i'?m going|i am going|chahti (hun|hoon|hu)|chahta (hun|hoon|hu)|karta hai|karti hai)\b", re.I)
ABUSE_HARMS = {"emotional_abuse", "physical_violence", "sexual_violence"}


def fix_typos(text: str) -> str:
    return _TYPO_RE.sub(lambda m: _TYPOS[m.group(0).lower()], text)


def journal_tier(text: str, classification) -> tuple[str, str | None]:
    """
    The tier for free text written in a journal, and a helplines card for it, if any.

    - Danger and emergency mean present or imminent risk to life. A clearly
      past-tense or third-person mention with nothing that says "now"
      ("I was suicidal years ago", "my friend tried") is mild. Any present
      signal, from the words or the classifier, or any doubt, keeps it danger.
    - Abuse or violence she discloses is not danger tier in a journal: the
      entry goes on, with a soft helplines card. A threat to life now stays danger.
      (SRS gap, for the clinical advisor.)
    """
    fixed = fix_typos(text)
    safety = getattr(classification, "safety", None)
    tier = higher_tier(assess_lexical(fixed), classification.risk_tier or "clear")
    card = "soft" if safety is not None and safety.discloses and safety.harm_type in ABUSE_HARMS else None
    if tier not in SUPPRESSING_TIERS:
        return tier, card
    if safety is not None and safety.danger_now != "no":
        return tier, card  # the classifier sees present danger, or can't rule it out
    if _PRESENT.search(fixed):
        return tier, card
    lexical = assess_lexical(fixed)
    if lexical not in SUPPRESSING_TIERS and safety is not None and safety.harm_type in ABUSE_HARMS:
        return lexical, card  # only the classifier rated it, and it is abuse, not danger now
    past = _PAST.search(fixed) or _OTHER.search(fixed) or (
        safety is not None and (safety.timing == "past" or safety.about == "someone_else"))
    return ("mild" if past else tier), card


def suppressed(entry: Entry | None) -> bool:
    # AI Chat keeps talking through a crisis: the helplines card sits under a warm reply.
    return bool(entry and entry.journal_type != "chat" and entry.crisis_tier in SUPPRESSING_TIERS)


TIER1, TIER2, CLARIFY = "tier1", "tier2", "clarify"
VIOLENCE = {"physical_violence", "sexual_violence"}
# Loaded by the safety check, not by the topic list.
SKILL_FOR_HARM = {"emotional_abuse": "abuse", "physical_violence": "abuse", "sexual_violence": "sexual_violence"}


def triage(tier: str, safety) -> str | None:
    """
    Runs before the skill is chosen. tier: assess() (self-harm rules + model);
    safety: the classifier's signals. Keywords never decide once the model has read
    the message; without the model, the rules do (fail safe).
    """
    if safety is None:
        return TIER1 if tier in SUPPRESSING_TIERS else TIER2 if tier == "mild" else None
    if safety.danger_now == "yes":
        return TIER1
    if tier in SUPPRESSING_TIERS and not (safety.timing == "past" and safety.danger_now == "no"):
        return TIER1
    if safety.harm_type == "none":
        return TIER2 if tier != "clear" else None
    if safety.about == "someone_else":
        return TIER2
    if safety.timing == "unclear" and safety.danger_now != "no":
        return CLARIFY
    return TIER2


def card_for(level: str | None, safety) -> str | None:
    """
    The helplines card under the reply: prominent, soft, or None. A message that
    itself tells of physical or sexual violence, past or present, gets one; so does
    ongoing abuse. Follow-ups ("I am safe now") don't. Anything else only once she
    shows distress now or asks for help.
    """
    if level == TIER1:
        return "prominent"
    if safety is None:
        return None
    if safety.discloses and safety.harm_type in VIOLENCE:
        return "prominent" if safety.timing in ("ongoing", "recent") else "soft"
    if safety.discloses and safety.harm_type == "emotional_abuse" and safety.timing == "ongoing":
        return "soft"
    if safety.distress_now or safety.asks_for_help:
        return "soft"
    return None


def pick(texts: dict, lang: str) -> str:
    """One language per message: Roman Urdu for Roman Urdu, mixed or Urdu-script writers."""
    return texts["english" if lang == language.ENGLISH else "roman_urdu"]


def _danger_variant(db: Session, account_id) -> str:
    """FR-CRIS-012: full the first time, abbreviated within 7 days of the last presentation."""
    recent = db.query(CrisisEvent).filter(
        CrisisEvent.account_id == account_id,
        CrisisEvent.tier == "danger",
        CrisisEvent.variant.isnot(None),
        CrisisEvent.created_at >= datetime.utcnow() - REPEAT_WINDOW,
    ).first()
    return "abbreviated" if recent else "full"


def record(
    db: Session, account_id, entry: Entry | None, field: str, tier: str, lang: str = language.ENGLISH
) -> CrisisResult | None:
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

    text = pick(CONTENT[tier][variant], lang) if variant else None
    return CrisisResult(tier=tier, variant=variant, text=text)


def mild_reference() -> str:
    return pick(CONTENT["mild"]["reference"], language.ENGLISH)
