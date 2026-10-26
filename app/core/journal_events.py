"""
journal_events: one row per finished journal entry, in one shape across the
four journals, for the combined report and the combined heatmap —
{user_id, module, entry_id, local_date, created_at, mood_signal}.

A function rather than a database view, so the tests (SQLite) and Postgres
read it the same way. The Saving, Thought and Exposure journals supply their
own rows; the check-in's are here.
"""
from collections import Counter
from datetime import date, timedelta

from sqlalchemy.orm import Session

from app.core import exposure_report, insights, savoring_report, streaks, thought_report
from app.models.entry import Entry


def checkin_events(db: Session, account_id, start: date, end: date) -> list[dict]:
    """Finished check-ins: mood_signal is the mood (1-5) on 0-10."""
    entries = db.query(Entry).filter(Entry.account_id == account_id, Entry.journal_type == "check_in",
                                     Entry.status == "completed", Entry.completed_at.isnot(None)).all()
    values = insights._values(db, entries)
    rows = []
    for e in entries:
        # A check-in from before the slots has no local_date: the phone's clock when it was finished gives the day.
        day = e.local_date or insights.local_date(e.completed_at, e.tz_offset or 0)
        if not start <= day <= end:
            continue
        mood = values.get(e.id, {}).get("mood")
        rows.append({"user_id": str(account_id), "module": "check_in", "entry_id": str(e.id),
                     "local_date": day.isoformat(),
                     "created_at": (e.completed_at + timedelta(minutes=e.tz_offset or 0)).isoformat(timespec="minutes"),
                     "mood_signal": round((mood - 1) * 2.5, 1) if isinstance(mood, int) else None})
    return rows


SOURCES = (checkin_events, savoring_report.journal_events, thought_report.journal_events,
           exposure_report.journal_events)


def events(db: Session, account_id, start: date, end: date) -> list[dict]:
    rows = [row for source in SOURCES for row in source(db, account_id, start, end)]
    return sorted(rows, key=lambda r: (r["local_date"], r["created_at"]))


def heatmap(db: Session, account_id, start: date, end: date, today: date) -> dict:
    """
    All four journals in one heatmap. A day's points are how many of the
    journals were used on it, not how many entries: three check-ins a day
    would otherwise make every day the darkest. Each day also says how many
    entries each journal had; the streak counts a day with any of them.
    """
    used: dict[date, Counter] = {}
    for row in events(db, account_id, date.min, today):
        used.setdefault(date.fromisoformat(row["local_date"]), Counter())[row["module"]] += 1
    out = streaks.heatmap({day: len(journals) for day, journals in used.items()}, start, end, today)
    for day in out["days"]:
        day["journals"] = dict(used.get(date.fromisoformat(day["date"]), {}))
    return out
