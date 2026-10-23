"""
Check-in slots: three a day, each with its own window on the user's clock.

    morning    05:00 – 11:59
    afternoon  12:00 – 17:59
    evening    18:00 – 04:59 (the next morning; it stays the evening's day)

A slot can be done inside its window. Once the window has passed it is
"missed" and can still be added (late, or yesterday's backfilled); anything
older than yesterday is closed. "Missed" is worked out from the clock, never
stored: an unfilled slot leaves no row behind.

Everything here is pure and takes the user's local time, so the server and the
tests reason about the same clock.
"""
from datetime import date, datetime, time, timedelta

MORNING, AFTERNOON, EVENING = "morning", "afternoon", "evening"
SLOTS = (MORNING, AFTERNOON, EVENING)

# (start, end) as times of day; the evening ends the next morning.
_WINDOWS = {
    MORNING: (time(5, 0), time(12, 0)),
    AFTERNOON: (time(12, 0), time(18, 0)),
    EVENING: (time(18, 0), time(5, 0)),
}

UPCOMING, OPEN, MISSED, CLOSED = "upcoming", "open", "missed", "closed"
# How many days back a missed slot can still be added: yesterday, and no further.
BACKFILL_DAYS = 1


def local_now(tz_offset: int, now_utc: datetime | None = None) -> datetime:
    """The user's wall-clock time, from minutes east of UTC."""
    return (now_utc or datetime.utcnow()) + timedelta(minutes=tz_offset)


def window(day: date, slot: str) -> tuple[datetime, datetime]:
    """When a slot opens and closes (end exclusive), on the user's clock."""
    start, end = _WINDOWS[slot]
    opens = datetime.combine(day, start)
    closes = datetime.combine(day + timedelta(days=1) if slot == EVENING else day, end)
    return opens, closes


def slot_at(moment: datetime) -> tuple[str, date]:
    """Which slot a local moment falls in, and the day it belongs to."""
    hour = moment.hour
    if 5 <= hour < 12:
        return MORNING, moment.date()
    if 12 <= hour < 18:
        return AFTERNOON, moment.date()
    if hour >= 18:
        return EVENING, moment.date()
    # 00:00–04:59 is still the evening before.
    return EVENING, moment.date() - timedelta(days=1)


def state(day: date, slot: str, moment: datetime) -> str:
    """upcoming | open | missed (can still be added) | closed, at a local moment."""
    opens, closes = window(day, slot)
    if moment < opens:
        return UPCOMING
    if moment < closes:
        return OPEN
    return MISSED if day >= moment.date() - timedelta(days=BACKFILL_DAYS) else CLOSED


def can_record(day: date, slot: str, moment: datetime) -> bool:
    return state(day, slot, moment) in (OPEN, MISSED)


def days_shown(moment: datetime) -> list[date]:
    """The days whose slots the app shows: yesterday and today, oldest first."""
    today = moment.date()
    return [today - timedelta(days=1), today]
