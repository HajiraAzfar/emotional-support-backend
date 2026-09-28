"""
Insights (SRS 4.10): what she recorded, counted and plotted.

Every view here is built from completed entries only (FR-INS-023), from this
account only (FR-CRIS-011, FR-INS-022), and describes what was recorded —
nothing here explains, predicts or advises (FR-INS-020).
"""
import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from app.core import capture, questionnaire
from app.models.captured_value import CapturedValue
from app.models.entry import Entry

# FR-INS-001: the periods the user can choose between.
PERIODS = {"7d": timedelta(days=7), "30d": timedelta(days=30), "3m": timedelta(days=90), "all": None}
DEFAULT_PERIOD = "30d"
# FR-INS-021: below this many points a view shows the placeholder instead.
MIN_POINTS = 3
# FR-INS-011: a change is only shown when the previous period has at least this many entries.
MIN_FOR_COMPARISON = 3
# A streak may keep one day of grace: if today has no entry yet but yesterday
# did, the streak is still alive. It only breaks once a whole day is missed.
STREAK_GRACE_DAYS = 1


def local_date(moment: datetime, tz_offset: int) -> date:
    """
    The calendar day this happened on for her, not for the server. Timestamps
    are stored in UTC; an entry written at 1am in Karachi belongs to that day,
    not to the one before it.
    """
    return (moment + timedelta(minutes=tz_offset)).date()


@dataclass
class Window:
    """The stretch of time a view covers, and the one before it for comparison."""
    period: str
    start: datetime | None
    end: datetime
    previous_start: datetime | None
    previous_end: datetime | None


def window_for(period: str, now: datetime | None = None) -> Window:
    now = now or datetime.utcnow()
    length = PERIODS[period]
    if length is None:
        return Window(period=period, start=None, end=now, previous_start=None, previous_end=None)
    start = now - length
    return Window(period=period, start=start, end=now, previous_start=start - length, previous_end=start)


def _completed(db: Session, account_id, start: datetime | None, end: datetime | None) -> list[Entry]:
    query = db.query(Entry).filter(Entry.account_id == account_id, Entry.status == "completed")
    if start is not None:
        query = query.filter(Entry.completed_at >= start)
    if end is not None:
        query = query.filter(Entry.completed_at <= end)
    return query.order_by(Entry.completed_at).all()


def _values(db: Session, entries: list[Entry]) -> dict:
    """entry id → {value_id: stored value}, skips excluded."""
    if not entries:
        return {}
    ids = [e.id for e in entries]
    rows = db.query(CapturedValue).filter(CapturedValue.entry_id.in_(ids)).all()
    out: dict = {}
    for row in rows:
        if row.skipped or row.value is None:
            continue
        out.setdefault(row.entry_id, {})[row.key] = json.loads(row.value)
    return out


def _label(library: str, item_id: str, user_terms: dict) -> str:
    return capture.LIBRARY_NAMES[library].get(item_id) or user_terms.get(item_id) or item_id


def _counts(entries, values, value_id: str, library: str, user_terms: dict) -> list[dict]:
    """
    FR-INS-006/008/010: how many entries each item was selected in, most first.
    Counts are entries, never percentages (FR-INS-007).
    """
    counted: dict[str, int] = {}
    for entry in entries:
        for item_id in values.get(entry.id, {}).get(value_id) or []:
            counted[item_id] = counted.get(item_id, 0) + 1
    return [
        {"id": item_id, "name": _label(library, item_id, user_terms), "count": count}
        for item_id, count in sorted(counted.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def build(db: Session, account_id, period: str, user_terms: dict, now: datetime | None = None,
          tz_offset: int = 0, weekly_goal: int | None = None) -> dict:
    """Every view for one period, in one pass over the entries."""
    now = now or datetime.utcnow()
    window = window_for(period, now)
    entries = _completed(db, account_id, window.start, window.end)
    values = _values(db, entries)

    # FR-INS-003/004/005: one point per recorded mood, no gaps filled in.
    mood_points = [
        {"date": local_date(entry.completed_at, tz_offset).isoformat(), "value": values[entry.id]["mood"], "entry_id": str(entry.id)}
        for entry in entries
        if "mood" in values.get(entry.id, {})
    ]

    # FR-INS-009: one point per recorded feeling intensity, the same way mood is
    # plotted. Check-in and thought records both carry it, so each point says
    # which journal it came from.
    intensity_points = [
        {
            "date": local_date(entry.completed_at, tz_offset).isoformat(),
            "value": values[entry.id]["feeling_intensity"],
            "entry_id": str(entry.id),
            "journal_type": entry.journal_type,
        }
        for entry in entries
        if "feeling_intensity" in values.get(entry.id, {})
    ]
    intensity_bounds = capture.scale_bounds("feeling_intensity") or (0, 10)
    mood_bounds = capture.scale_bounds("mood") or (1, 5)

    traps = _counts(entries, values, "thinking_traps", "thinking_traps", user_terms)
    triggers = _counts(entries, values, "triggers", "triggers", user_terms)
    feelings = _counts(entries, values, "feelings", "feelings", user_terms)

    # FR-INS-011: change against the period just before, when there is enough there to compare.
    if window.previous_start is not None:
        earlier = _completed(db, account_id, window.previous_start, window.previous_end)
        if len(earlier) >= MIN_FOR_COMPARISON:
            earlier_values = _values(db, earlier)
            for rows, value_id, library in (
                (traps, "thinking_traps", "thinking_traps"),
                (triggers, "triggers", "triggers"),
                (feelings, "feelings", "feelings"),
            ):
                before = _counts(earlier, earlier_values, value_id, library, user_terms)
                previous_counts = {row["id"]: row["count"] for row in before}
                for row in rows:
                    row["change"] = row["count"] - previous_counts.get(row["id"], 0)

    # FR-INS-015: a day is marked when at least one entry was completed on it.
    # Each day also carries the mood she recorded, so the calendar can show it;
    # a day with entries but no mood (a free write) is marked with mood null.
    by_day: dict[str, list[int]] = {}
    for entry in entries:
        day = local_date(entry.completed_at, tz_offset).isoformat()
        moods = by_day.setdefault(day, [])
        mood = values.get(entry.id, {}).get("mood")
        if mood is not None:
            moods.append(mood)
    days = [
        {"date": day, "mood": round(sum(moods) / len(moods)) if moods else None,
         "entries": sum(1 for e in entries if local_date(e.completed_at, tz_offset).isoformat() == day)}
        for day, moods in sorted(by_day.items())
    ]

    # FR-INS-016: how many entries of each journal type.
    types: dict[str, int] = {}
    for entry in entries:
        types[entry.journal_type] = types.get(entry.journal_type, 0) + 1

    return {
        "period": period,
        "from": local_date(window.start, tz_offset).isoformat() if window.start else None,
        "to": local_date(window.end, tz_offset).isoformat(),
        "completed_entries": len(entries),
        "min_points": MIN_POINTS,
        # FR-INS-021: the client shows the placeholder while there are too few points.
        "mood": {
            "points": mood_points,
            "needed": max(0, MIN_POINTS - len(mood_points)),
            # The ends of the scale, so the chart never hardcodes them.
            "min": mood_bounds[0],
            "max": mood_bounds[1],
        },
        "feeling_intensity": {
            "points": intensity_points,
            "needed": max(0, MIN_POINTS - len(intensity_points)),
            "min": intensity_bounds[0],
            "max": intensity_bounds[1],
        },
        "triggers": triggers,
        "feelings": feelings,
        "thinking_traps": traps,
        # The trends: what moved, week by week, rather than one number per period.
        "mood_trend": _mood_trend(entries, values, tz_offset, mood_bounds),
        "mood_distribution": _distribution(entries, values, "mood_5"),
        "trap_trend": _item_trend(entries, values, "thinking_traps", "thinking_traps", user_terms, tz_offset),
        "trigger_trend": _item_trend(entries, values, "triggers", "triggers", user_terms, tz_offset),
        "writing_times": _writing_times(entries, tz_offset),
        "trend_min_weeks": TREND_MIN_WEEKS,
        "exposure": _exposure(entries, values, tz_offset),
        "calendar": {"days": days},
        # FR-INS-015 with module 4: days in a row, counted over her whole history.
        "streak": _streak(db, account_id, tz_offset, now, weekly_goal),
        # FR-INS-017/019: whether to offer it, and the line with no numbers on it.
        "questionnaire": {
            **questionnaire.status(db, account_id, now),
            "trend": questionnaire.trend(db, account_id, window.start, window.end),
        },
        "journal_types": [
            {"journal_type": name, "count": count}
            for name, count in sorted(types.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
    }


# Trends are drawn per week: day-to-day mood is noise, and a week is short
# enough to still be about now.
TREND_MIN_WEEKS = 2
# How many thinking traps a trend chart can carry before it is unreadable.
TREND_TOP_N = 3
# Parts of the day, by local hour, for "when you write".
DAY_PARTS = [("morning", "Morning", 5, 12), ("afternoon", "Afternoon", 12, 17),
             ("evening", "Evening", 17, 22), ("night", "Night", 22, 5)]


def _week_start(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _mood_trend(entries, values, tz_offset: int, bounds: tuple[int, int]) -> dict:
    """
    FR-INS-003 as a trend: the average mood of each week she wrote in. Weeks
    without an entry are left out rather than drawn as zero — she was not at
    zero, she simply did not write.
    """
    weeks: dict[date, list[int]] = {}
    for entry in entries:
        mood = values.get(entry.id, {}).get("mood")
        if mood is not None:
            weeks.setdefault(_week_start(local_date(entry.completed_at, tz_offset)), []).append(mood)
    points = [
        {"week_start": week.isoformat(), "value": round(sum(moods) / len(moods), 2), "entries": len(moods)}
        for week, moods in sorted(weeks.items())
    ]
    return {
        "points": points,
        "min": bounds[0],
        "max": bounds[1],
        "needed": max(0, TREND_MIN_WEEKS - len(points)),
    }


def _distribution(entries, values, scale_name: str) -> list[dict]:
    """How many entries sat at each point of the mood scale (FR-INS-007: counts)."""
    counts: dict[int, int] = {}
    for entry in entries:
        mood = values.get(entry.id, {}).get("mood")
        if mood is not None:
            counts[mood] = counts.get(mood, 0) + 1
    return [
        {"value": option["value"], "label": option["label"], "count": counts.get(option["value"], 0)}
        for option in capture.SCALES[scale_name]
    ]


def _item_trend(entries, values, value_id: str, library: str, user_terms: dict, tz_offset: int) -> dict:
    """
    The thinking patterns (or triggers) she noted most, week by week — this is
    the view that answers "is this happening less than it was?". Only the few
    most frequent are drawn; the rest would be unreadable.
    """
    weeks: dict[date, dict[str, int]] = {}
    totals: dict[str, int] = {}
    for entry in entries:
        week = _week_start(local_date(entry.completed_at, tz_offset))
        bucket = weeks.setdefault(week, {})
        for item_id in values.get(entry.id, {}).get(value_id) or []:
            bucket[item_id] = bucket.get(item_id, 0) + 1
            totals[item_id] = totals.get(item_id, 0) + 1

    ordered_weeks = sorted(weeks)
    top = [item for item, _ in sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))[:TREND_TOP_N]]
    return {
        "weeks": [week.isoformat() for week in ordered_weeks],
        "series": [
            {
                "id": item_id,
                "name": _label(library, item_id, user_terms),
                "counts": [weeks[week].get(item_id, 0) for week in ordered_weeks],
            }
            for item_id in top
        ],
        "needed": max(0, TREND_MIN_WEEKS - len(ordered_weeks)),
    }


def _writing_times(entries, tz_offset: int) -> list[dict]:
    """When in the day she tends to write. Behaviour, not mood."""
    counts = {code: 0 for code, _, _, _ in DAY_PARTS}
    for entry in entries:
        hour = (entry.completed_at + timedelta(minutes=tz_offset)).hour
        for code, _label_text, start, end in DAY_PARTS:
            inside = start <= hour < end if start < end else (hour >= start or hour < end)
            if inside:
                counts[code] += 1
                break
    return [{"part": code, "label": label, "count": counts[code]} for code, label, _, _ in DAY_PARTS]


def _streak(db: Session, account_id, tz_offset: int, now: datetime, weekly_goal: int | None) -> dict:
    """
    Days in a row with at least one completed entry, counted in her own
    timezone and over her whole history — a streak that reset every time she
    changed the period control would mean nothing.

    Missing today does not break it (STREAK_GRACE_DAYS): she may simply not
    have written yet. Missing a whole day does.
    """
    rows = (
        db.query(Entry.completed_at)
        .filter(Entry.account_id == account_id, Entry.status == "completed", Entry.completed_at.isnot(None))
        .all()
    )
    days = sorted({local_date(row[0], tz_offset) for row in rows})
    today = local_date(now, tz_offset)

    longest = run = 0
    previous = None
    for day in days:
        run = run + 1 if previous is not None and (day - previous).days == 1 else 1
        longest = max(longest, run)
        previous = day

    current = 0
    if days:
        missed = (today - days[-1]).days
        if missed <= STREAK_GRACE_DAYS:
            current, expected = 1, days[-1]
            for day in reversed(days[:-1]):
                expected -= timedelta(days=1)
                if day != expected:
                    break
                current += 1

    # The week she is in now, Monday to Sunday, against the goal she set.
    week_start = today - timedelta(days=today.weekday())
    this_week = sum(1 for day in days if day >= week_start)
    # Entries, not days: the ring counts everything she has ever finished.
    completed_days = [local_date(row[0], tz_offset) for row in rows]
    this_month = sum(1 for day in completed_days if (day.year, day.month) == (today.year, today.month))

    return {
        "current": current,
        "longest": longest,
        "today": bool(days) and days[-1] == today,
        "week_start": week_start.isoformat(),
        "days_this_week": this_week,
        "weekly_goal": weekly_goal,
        "total_entries": len(rows),
        "entries_this_month": this_month,
    }


def _exposure(entries: list[Entry], values: dict, tz_offset: int = 0) -> list[dict]:
    """
    FR-INS-013/014: for each feared outcome, the distress of every completed
    cycle in order — shown only where two or more cycles are complete.
    """
    grouped: dict[str, list[dict]] = {}
    for entry in entries:
        if entry.journal_type != "exposure":
            continue
        recorded = values.get(entry.id, {})
        outcome = (recorded.get("feared_outcome") or "").strip()
        if not outcome:
            continue
        grouped.setdefault(outcome.lower(), []).append({
            "entry_id": str(entry.id),
            "feared_outcome": outcome,
            "completed_at": local_date(entry.completed_at, tz_offset).isoformat(),
            "before": recorded.get("distress_before"),
            "during": recorded.get("distress_during"),
            "after": recorded.get("distress_after"),
        })

    out = []
    for cycles in grouped.values():
        cycles.sort(key=lambda c: c["completed_at"])
        for number, cycle in enumerate(cycles, start=1):
            cycle["cycle"] = number
        out.append({
            "feared_outcome": cycles[0]["feared_outcome"],
            "cycles": cycles,
            # FR-INS-014: one cycle alone is not a comparison.
            "enough": len(cycles) >= 2,
        })
    return sorted(out, key=lambda group: (-len(group["cycles"]), group["feared_outcome"]))