"""
Check-in slots: three a day, each with its own window on the user's clock.

    morning    05:00 – 11:59
    afternoon  12:00 – 17:59
    evening    18:00 – 04:59 (the next morning; it stays the evening's day)

A slot can be done only inside its window: once it has passed, it is closed,
whether or not it was filled in (no backfill, so a day's report never changes
after the day is over). An unfilled slot leaves no row behind.

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

UPCOMING, OPEN, CLOSED = "upcoming", "open", "closed"


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
    """upcoming | open | closed, at a local moment."""
    opens, closes = window(day, slot)
    if moment < opens:
        return UPCOMING
    if moment < closes:
        return OPEN
    return CLOSED


def can_record(day: date, slot: str, moment: datetime) -> bool:
    return state(day, slot, moment) == OPEN


def day_over(day: date, moment: datetime) -> bool:
    """A day is over once its evening has closed: 05:00 the next morning."""
    return moment >= window(day, EVENING)[1]


def days_shown(moment: datetime) -> list[date]:
    """The days whose slots the app shows (yesterday's to read): yesterday and today, oldest first."""
    today = moment.date()
    return [today - timedelta(days=1), today]
