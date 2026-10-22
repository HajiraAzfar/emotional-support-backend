"""
Badges: what she did, never how she felt.

Every rule below reads completed entries and their recorded values. None of
them looks at mood, distress, or a run of consecutive days — nothing here can
be earned by feeling better or lost by missing a week, and nothing implies
that anything has been treated or cured. The model is not involved: it cannot
award a badge, and a regenerated report cannot award one twice.
"""
import json
from datetime import date
from pathlib import Path
from typing import Callable

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core import insights
from app.models.earned_badge import EarnedBadge
from app.models.entry import Entry

_CONTENT = Path(__file__).resolve().parent.parent / "content" / "badges.json"
_FILE = json.loads(_CONTENT.read_text(encoding="utf-8"))
DEFINITIONS: list[dict] = _FILE["badges"]
BY_ID: dict[str, dict] = {b["id"]: b for b in DEFINITIONS}

# The four journals with a capture schedule that a person chooses to open.
JOURNALS = ("check_in", "savouring", "thought", "exposure")
# Where a person writes something she worked out for herself. Used by
# "Learning to Notice"; each entry counts once however many of these it holds.
LEARNING_VALUES = ("learning", "significance", "revised_thought")

# A rule sees every completed entry and its values, and returns the entries
# that qualify her. An empty list means not earned yet.
Rule = Callable[[list[Entry], dict], list[Entry]]


def _text(values: dict, entry: Entry, key: str) -> str:
    recorded = values.get(entry.id, {}).get(key)
    return recorded.strip() if isinstance(recorded, str) else ""


def _of_type(entries: list[Entry], journal_type: str) -> list[Entry]:
    return [e for e in entries if e.journal_type == journal_type]


def _first_steps(entries, values) -> list[Entry]:
    return entries[:1]


def _finding_my_rhythm(entries, values) -> list[Entry]:
    # Three different days, in any order and with any gaps between them.
    seen: dict[date, Entry] = {}
    for entry in entries:
        seen.setdefault(entry.completed_at.date(), entry)
    return list(seen.values())[:3] if len(seen) >= 3 else []


def _four_ways(entries, values) -> list[Entry]:
    one_each = [first for first in (next(iter(_of_type(entries, t)), None) for t in JOURNALS) if first]
    return one_each if len(one_each) == len(JOURNALS) else []


def _thought_explorer(entries, values) -> list[Entry]:
    return _of_type(entries, "thought")[:1]


def _another_perspective(entries, values) -> list[Entry]:
    return [e for e in _of_type(entries, "thought") if _text(values, e, "revised_thought")][:1]


def _pattern_noticer(entries, values) -> list[Entry]:
    # The same pattern in two separate records — one record naming two patterns
    # is noticing two things once, not one thing twice.
    where: dict[str, list[Entry]] = {}
    for entry in _of_type(entries, "thought"):
        for trap in values.get(entry.id, {}).get("thinking_traps") or []:
            where.setdefault(trap, []).append(entry)
    repeated = [found for found in where.values() if len(found) >= 2]
    return repeated[0][:2] if repeated else []


def _moment_keeper(entries, values) -> list[Entry]:
    return _of_type(entries, "savouring")[:1]


def _learning_to_notice(entries, values) -> list[Entry]:
    wrote = [e for e in entries if any(_text(values, e, key) for key in LEARNING_VALUES)]
    return wrote[:3] if len(wrote) >= 3 else []


def _brave_first_step(entries, values) -> list[Entry]:
    return _of_type(entries, "exposure")[:1]


RULES: dict[str, Rule] = {
    "first_steps": _first_steps,
    "finding_my_rhythm": _finding_my_rhythm,
    "four_ways_to_reflect": _four_ways,
    "thought_explorer": _thought_explorer,
    "another_perspective": _another_perspective,
    "pattern_noticer": _pattern_noticer,
    "moment_keeper": _moment_keeper,
    "learning_to_notice": _learning_to_notice,
    "brave_first_step": _brave_first_step,
}

# Every badge in the content file must have a rule, and every rule a badge:
# a badge nobody can earn, or a rule nothing describes, is a bug worth seeing
# at startup rather than in a report.
assert set(RULES) == set(BY_ID), f"badges.json and RULES disagree: {set(RULES) ^ set(BY_ID)}"


def earned(db: Session, account_id) -> dict[str, EarnedBadge]:
    rows = db.query(EarnedBadge).filter(EarnedBadge.account_id == account_id).all()
    return {row.badge_id: row for row in rows}


def evaluate(db: Session, account_id) -> list[str]:
    """
    Awards every badge she now qualifies for and returns the ids of the ones
    awarded by this call — which is what the weekly report shows as new.

    Safe to call as often as you like: a badge already held is skipped, and the
    unique constraint catches the race if two calls land together.
    """
    already = earned(db, account_id)
    outstanding = [badge_id for badge_id in RULES if badge_id not in already]
    if not outstanding:
        return []

    # Whole history, oldest first: badges are lifetime, not per week.
    entries = insights._completed(db, account_id, None, None)
    if not entries:
        return []
    values = insights._values(db, entries)

    awarded = []
    for badge_id in outstanding:
        qualifying = RULES[badge_id](entries, values)
        if not qualifying:
            continue
        db.add(EarnedBadge(
            account_id=account_id,
            badge_id=badge_id,
            entry_ids=",".join(str(e.id) for e in qualifying),
        ))
        try:
            db.flush()
        except IntegrityError:
            # Awarded by a request that ran alongside this one. Not an error.
            db.rollback()
            continue
        awarded.append(badge_id)
    if awarded:
        db.commit()
    return awarded


def as_dict(badge_id: str, row: EarnedBadge | None) -> dict:
    """One badge for the client: its definition, and whether she has it yet."""
    definition = BY_ID[badge_id]
    return {
        **definition,
        "earned": row is not None,
        "earned_at": row.earned_at.isoformat() if row else None,
        "entry_ids": (row.entry_ids.split(",") if row and row.entry_ids else []),
    }


def all_for(db: Session, account_id) -> list[dict]:
    """Every badge, earned first, so the screen can show both states."""
    held = earned(db, account_id)
    rows = [as_dict(badge_id, held.get(badge_id)) for badge_id in BY_ID]
    return sorted(rows, key=lambda b: (not b["earned"], b["earned_at"] or ""))
