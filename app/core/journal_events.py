"""
journal_events: one row per finished journal entry, in one shape across the
journals, for the combined clinical report —
{user_id, module, entry_id, local_date, created_at, mood_signal}.

A function rather than a database view, so the tests (SQLite) and Postgres
read it the same way. The Saving, Thought and Exposure journals supply their
rows; the check-in adds its own when the combined report is built.
"""
from datetime import date

from sqlalchemy.orm import Session

from app.core import exposure_report, savoring_report, thought_report

SOURCES = (savoring_report.journal_events, thought_report.journal_events, exposure_report.journal_events)


def events(db: Session, account_id, start: date, end: date) -> list[dict]:
    rows = [row for source in SOURCES for row in source(db, account_id, start, end)]
    return sorted(rows, key=lambda r: (r["local_date"], r["created_at"]))
