"""
"Your Week in Reflection": one week of her own entries, counted rather than
guessed.

Everything here is deterministic. The counts, the points on the graphs, the
cards and the badges are read out of what she saved; the only generated text
is the narrative in `narrative.py`, and that is handed the output of this
module so it cannot invent a number. A week with nothing in it says so.

Weeks are local Monday to Sunday. The window is built from her timezone, so an
entry written at 1am on Monday belongs to that Monday wherever the server is.
"""
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from app.core import badges, capture, insights
from app.models.entry import Entry
from app.models.weekly_reflection import WeeklyReflection

# How many weeks back the history list goes. Far enough to look over a couple
# of months, short enough that the list is still a list.
HISTORY_WEEKS = 12
# A card is a line she wrote, not an essay: enough to recognise the entry by.
CARD_CHARS = 220

# Where each journal records something she worked out, and how to introduce it
# without claiming it helped — she said it, the app does not grade it.
HELPED_SOURCES = [
    ("savouring", "significance", "A moment worth keeping", "Why this one mattered to you."),
    ("thought", "revised_thought", "A perspective worth remembering", "A more balanced way you put it."),
    ("exposure", "learning", "Something you noticed", "What you took from the step you tried."),
]


def week_start_for(day: date) -> date:
    """The Monday of the week a local date falls in."""
    return day - timedelta(days=day.weekday())


def current_week_start(tz_offset: int, now: datetime | None = None) -> date:
    return week_start_for(insights.local_date(now or datetime.utcnow(), tz_offset))


def window(week_start: date, tz_offset: int) -> tuple[datetime, datetime]:
    """
    The UTC instants that bracket a local week: Monday 00:00 local up to, but
    not including, the next Monday 00:00 local. Half-open, so an entry can
    never land in two weeks and never fall between them.
    """
    local_midnight = datetime.combine(week_start, datetime.min.time())
    start = local_midnight - timedelta(minutes=tz_offset)
    return start, start + timedelta(days=7)


def _completed_in(db: Session, account_id, start: datetime, end: datetime) -> list[Entry]:
    # insights._completed is inclusive at both ends, which would let an entry
    # at exactly midnight count in two weeks. Trim the last instant instead.
    return insights._completed(db, account_id, start, end - timedelta(microseconds=1))


def _non_empty(values: dict, entry: Entry, key: str) -> str:
    recorded = values.get(entry.id, {}).get(key)
    return recorded.strip() if isinstance(recorded, str) and recorded.strip() else ""


def _overview(entries: list[Entry], values: dict) -> dict:
    counts = {journal: 0 for journal in badges.JOURNALS}
    for entry in entries:
        if entry.journal_type in counts:
            counts[entry.journal_type] += 1

    # FR-JRN-006: a step she planned is not a step she tried. Trying it is
    # recorded afterwards — an account of how it went, or a distress rating
    # from during or after.
    exposures = [e for e in entries if e.journal_type == "exposure"]
    attempted = [
        e for e in exposures
        if _non_empty(values, e, "post_account")
        or values.get(e.id, {}).get("distress_during") is not None
        or values.get(e.id, {}).get("distress_after") is not None
    ]
    learnings = [
        e for e in entries
        if any(_non_empty(values, e, key) for key in badges.LEARNING_VALUES)
    ]
    return {
        "entries": len(entries),
        "check_in": counts["check_in"],
        "thought": counts["thought"],
        "savouring": counts["savouring"],
        "exposure_planned": len(exposures),
        "exposure_attempted": len(attempted),
        "learnings": len(learnings),
    }


def _helped(entries: list[Entry], values: dict, tz_offset: int) -> list[dict]:
    """
    What she wrote down for herself this week. Each card is her own words and
    a way back to the entry — never a claim that it worked, because only she
    can say that.
    """
    cards = []
    for journal_type, key, title, blurb in HELPED_SOURCES:
        for entry in entries:
            if entry.journal_type != journal_type:
                continue
            text = _non_empty(values, entry, key)
            if not text:
                continue
            cards.append({
                "kind": key,
                "title": title,
                "blurb": blurb,
                "text": text[:CARD_CHARS],
                "entry_id": str(entry.id),
                "journal_type": journal_type,
                "date": insights.local_date(entry.completed_at, tz_offset).isoformat(),
            })
    return cards


def _wins(overview: dict) -> list[dict]:
    """
    Small wins are actions, counted. Nothing here depends on her mood being
    good or her week being unbroken, and a week with one entry has one win.
    """
    possible = [
        ("check_in", overview["check_in"], "Checked in with yourself", "check-in", "check-ins"),
        ("thought", overview["thought"], "Worked through a thought", "thought record", "thought records"),
        ("savouring", overview["savouring"], "Held on to something good", "moment saved", "moments saved"),
        ("exposure", overview["exposure_attempted"], "Tried a step you had been avoiding", "step", "steps"),
        ("learning", overview["learnings"], "Wrote down something you noticed", "learning", "learnings"),
    ]
    return [
        {"id": key, "title": title, "count": count,
         "detail": f"{count} {one if count == 1 else many} this week"}
        for key, count, title, one, many in possible if count
    ]


def _journal_detail(entries: list[Entry], values: dict, user_terms: dict, tz_offset: int) -> dict:
    """Each journal's own story, from its own fields (spec section 9)."""
    savouring = [e for e in entries if e.journal_type == "savouring"]
    thought = [e for e in entries if e.journal_type == "thought"]
    exposure = [e for e in entries if e.journal_type == "exposure"]

    return {
        "check_in": {
            "entries": len([e for e in entries if e.journal_type == "check_in"]),
            "triggers": insights._counts(entries, values, "triggers", "triggers", user_terms),
        },
        "savouring": {
            "entries": len(savouring),
            "moments": [
                {"entry_id": str(e.id),
                 "date": insights.local_date(e.completed_at, tz_offset).isoformat(),
                 "event": _non_empty(values, e, "event")[:CARD_CHARS],
                 "significance": _non_empty(values, e, "significance")[:CARD_CHARS],
                 "feelings": [insights._label("feelings", f, user_terms)
                              for f in values.get(e.id, {}).get("feelings") or []]}
                for e in savouring
            ],
        },
        "thought": {
            "entries": len(thought),
            "patterns": insights._counts(thought, values, "thinking_traps", "thinking_traps", user_terms),
            "balanced_thoughts": sum(1 for e in thought if _non_empty(values, e, "revised_thought")),
            # Her own two ratings of the same feeling, before the work and after.
            # Kept apart from the counts above: one is what she recorded, the
            # other is arithmetic on it.
            "rating_shifts": insights._rating_shifts(thought, values, user_terms),
        },
        "exposure": {
            # Planned, tried and what she learned are three different things.
            "planned": len(exposure),
            "attempted": sum(
                1 for e in exposure
                if _non_empty(values, e, "post_account")
                or values.get(e.id, {}).get("distress_during") is not None
                or values.get(e.id, {}).get("distress_after") is not None
            ),
            "cycles": insights._exposure(entries, values, tz_offset),
        },
    }


def reflection_for(db: Session, account_id, week_start: date) -> str | None:
    row = (db.query(WeeklyReflection)
             .filter(WeeklyReflection.account_id == account_id,
                     WeeklyReflection.week_start == week_start)
             .first())
    return row.text if row else None


def save_reflection(db: Session, account_id, week_start: date, text: str) -> str | None:
    """Writes, replaces, or (on empty text) removes the line she carries forward."""
    row = (db.query(WeeklyReflection)
             .filter(WeeklyReflection.account_id == account_id,
                     WeeklyReflection.week_start == week_start)
             .first())
    text = text.strip()
    if not text:
        if row:
            db.delete(row)
            db.commit()
        return None
    if row:
        row.text = text
    else:
        db.add(WeeklyReflection(account_id=account_id, week_start=week_start, text=text))
    db.commit()
    return text


def build(db: Session, account_id, week_start: date, user_terms: dict,
          tz_offset: int = 0, now: datetime | None = None) -> dict:
    """One week, entirely from saved entries. No narrative: that is added on top."""
    now = now or datetime.utcnow()
    start, end = window(week_start, tz_offset)
    entries = _completed_in(db, account_id, start, end)
    values = insights._values(db, entries)

    mood_bounds = capture.scale_bounds("mood") or (1, 5)
    intensity_bounds = capture.scale_bounds("feeling_intensity") or (0, 10)
    point = lambda entry, key: {
        "date": insights.local_date(entry.completed_at, tz_offset).isoformat(),
        "value": values[entry.id][key],
        "entry_id": str(entry.id),
        "journal_type": entry.journal_type,
    }

    overview = _overview(entries, values)
    # Awarded from her whole history, but only the ones that landed now are new.
    new_badges = badges.evaluate(db, account_id)
    held = badges.earned(db, account_id)

    return {
        "week_start": week_start.isoformat(),
        "week_end": (week_start + timedelta(days=6)).isoformat(),
        "is_current": week_start == current_week_start(tz_offset, now),
        "overview": overview,
        # Missing days stay missing: one point per rating, nothing filled in.
        "mood": {
            "points": [point(e, "mood") for e in entries if "mood" in values.get(e.id, {})],
            "min": mood_bounds[0], "max": mood_bounds[1],
        },
        "feeling_intensity": {
            "points": [point(e, "feeling_intensity") for e in entries
                       if "feeling_intensity" in values.get(e.id, {})],
            "min": intensity_bounds[0], "max": intensity_bounds[1],
        },
        # How often each was recorded — a count, never a severity.
        "feelings": insights._counts(entries, values, "feelings", "feelings", user_terms),
        "triggers": insights._counts(entries, values, "triggers", "triggers", user_terms),
        "thinking_traps": insights._counts(entries, values, "thinking_traps", "thinking_traps", user_terms),
        "journals": _journal_detail(entries, values, user_terms, tz_offset),
        "helped": _helped(entries, values, tz_offset),
        "wins": _wins(overview),
        "badges": {
            "new": [badges.as_dict(badge_id, held.get(badge_id)) for badge_id in new_badges],
            "earned_total": len(held),
            "total": len(badges.BY_ID),
        },
        "reflection": reflection_for(db, account_id, week_start),
        # Last week's line, so she can see what she meant to carry in.
        "carried_in": reflection_for(db, account_id, week_start - timedelta(days=7)),
    }


def history(db: Session, account_id, tz_offset: int = 0, now: datetime | None = None) -> list[dict]:
    """
    The weeks she can open, newest first — only those with something in them,
    plus the current one so there is always somewhere to land.
    """
    now = now or datetime.utcnow()
    this_week = current_week_start(tz_offset, now)
    earliest = this_week - timedelta(weeks=HISTORY_WEEKS - 1)
    start, _ = window(earliest, tz_offset)
    _, end = window(this_week, tz_offset)

    counts: dict[date, int] = {}
    for entry in _completed_in(db, account_id, start, end):
        day = insights.local_date(entry.completed_at, tz_offset)
        counts[week_start_for(day)] = counts.get(week_start_for(day), 0) + 1

    weeks = sorted(set(counts) | {this_week}, reverse=True)
    return [
        {"week_start": week.isoformat(),
         "week_end": (week + timedelta(days=6)).isoformat(),
         "entries": counts.get(week, 0),
         "is_current": week == this_week}
        for week in weeks
    ]
