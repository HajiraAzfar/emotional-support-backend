"""
The Saving Journal (savouring): noticing, appreciating and extending a good
moment — one that just happened, one remembered, or one looked forward to.
Depth over volume: a few moments, actually felt.

Stored on the shared engine: an entries row (journal_type = 'savouring',
local_date, is_favorite, event_date, source_entry_id, client_id) and one
captured_values row per answer, under the value ids below. The app owns the
steps (fixed wording, one submit), so a quick moment takes seconds and can be
kept offline until it syncs; the server checks everything it is sent.

Rules: at most CAP moments per day on her clock, dated today or yesterday,
and nothing after "what happened" is required. Hitting the cap is a friendly
refusal the app shows as such, never an error.

Also here: the heatmap and streak (a day counts with any moment; one grace
day per rolling week), the weekly memory lane, the "how was it really?"
follow-up of an anticipated moment, and the gentle support signal when the
moments keep ending very low.
"""
import json
import random
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from app.core import capture, insights, slots, streaks as shared_streaks
from app.models.captured_value import CapturedValue
from app.models.entry import Entry

JOURNAL = "savouring"
MOMENT, REMINISCE, ANTICIPATE = "moment", "reminisce", "anticipate"
TYPES = (MOMENT, REMINISCE, ANTICIPATE)
QUICK, FULL = "quick", "full"

# At most this many moments per local day, and how many days back one may be dated.
CAP = 3
BACKFILL_DAYS = 1
# A day's heatmap points: a quick moment is one, a full one two.
POINTS = {QUICK: 1, FULL: 2}
# An anticipated moment may be up to a year ahead, and is followed up for a month.
EVENT_AHEAD_DAYS = 366
FOLLOW_UP_DAYS = 30
# Memory lane: moments at least a week old, two or three of them.
MEMORY_AGE_DAYS, MEMORY_POOL, MEMORY_SHOWN = 7, 10, 3
# Support: this many moments in a row ending at or below LOW_MOOD suggests a
# break; with a low week of check-ins as well, reaching out for support.
LOW_MOOD, LOW_RUN = 2, 5
CHECKIN_LOW, CHECKIN_DAYS, CHECKIN_MIN = 2.0, 7, 3

# Value ids (captured_values.key). "event", "significance" and "feelings" are
# the journal's ids from before this module, so older moments read the same.
V_TYPE, V_MODE = "savor_type", "savor_mode"
V_WHAT, V_CATEGORIES, V_PEOPLE, V_PLACES = "event", "categories", "people", "places"
V_MOOD_BEFORE, V_FEELINGS, V_RATINGS = "mood_before", "feelings", "feeling_ratings"
V_SENSORY, V_MOOD_AFTER, V_TECHNIQUES = "sensory_note", "mood_after", "techniques"
V_WHY, V_SOUVENIR = "significance", "souvenir"
V_EXPECTED, V_ACTUAL = "expected_rating", "actual_rating"
# Everything an edit replaces: all but how it was captured and the follow-up.
CONTENT_VALUES = (V_TYPE, V_WHAT, V_CATEGORIES, V_PEOPLE, V_PLACES, V_MOOD_BEFORE, V_FEELINGS, V_RATINGS,
                  V_SENSORY, V_MOOD_AFTER, V_TECHNIQUES, V_WHY, V_SOUVENIR, V_EXPECTED)

LIMITS = {V_WHAT: 500, V_SENSORY: 300, V_WHY: 500, V_SOUVENIR: 120}
TAG_LENGTH, TAGS_MAX, CATEGORIES_MAX, EMOTIONS_MAX = 40, 3, 3, 12

CATEGORIES, TECHNIQUES, FEELINGS = "savoring_categories", "savoring_techniques", "feelings"
# The feelings a moment can be felt with: the library's positive categories.
POSITIVE_FEELINGS = {
    item["id"]
    for cat in capture.LIBRARIES[FEELINGS]["categories"] if cat.get("valence") == "positive"
    for item in cat["items"]
}


class Refused(Exception):
    """A moment the rules do not allow; `code` and `message` go to the app as they are."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def cap_message(day: date, today: date) -> str:
    if day == today:
        return f"You've captured {CAP} good moments today. Come back tomorrow for more."
    return f"You've already captured {CAP} good moments for that day."


# ---------- reading ----------

def _text(value) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _tags(values: list[str]) -> list[str]:
    """Short words for who or where, each once (ignoring case), in her order."""
    out, seen = [], set()
    for value in values:
        tag = " ".join(str(value).split())
        if not tag:
            continue
        if len(tag) > TAG_LENGTH:
            raise ValueError(f"A tag is at most {TAG_LENGTH} characters")
        if tag.casefold() not in seen:
            seen.add(tag.casefold())
            out.append(tag)
    return out


def _ids(values: list[str], allowed, what: str) -> list[str]:
    out = []
    for value in values:
        if value not in allowed:
            raise ValueError(f"Unknown {what}: {value}")
        if value not in out:
            out.append(value)
    return out


def validate(content, terms: dict[str, str], local_date: date) -> dict:
    """
    Value id → value, for what she gave. Raises ValueError for anything the app
    should never have sent. A moment she looks forward to keeps its expected
    rating and day; the other two kinds drop them.
    """
    what = _text(content.what_happened)
    if what is None:
        raise ValueError("Say a few words about what happened")
    emotions = {}
    for feeling, strength in content.emotions.items():
        if feeling not in capture.LIBRARY_NAMES[FEELINGS] and feeling not in terms:
            raise ValueError(f"Unknown feeling: {feeling}")
        emotions[feeling] = strength
    values = {
        V_TYPE: content.type,
        V_WHAT: what,
        V_CATEGORIES: _ids(content.categories, capture.LIBRARY_NAMES[CATEGORIES], "category"),
        V_PEOPLE: _tags(content.people),
        V_PLACES: _tags(content.places),
        V_MOOD_BEFORE: content.mood_before,
        V_FEELINGS: list(emotions),
        V_RATINGS: {k: v for k, v in emotions.items() if v is not None},
        V_SENSORY: _text(content.sensory_note),
        V_MOOD_AFTER: content.mood_after,
        V_TECHNIQUES: _ids(content.techniques, capture.LIBRARY_NAMES[TECHNIQUES], "technique"),
        V_WHY: _text(content.why_good),
        V_SOUVENIR: _text(content.souvenir_line),
        V_EXPECTED: content.expected_rating if content.type == ANTICIPATE else None,
    }
    if content.type == ANTICIPATE and content.event_date is not None:
        if not local_date <= content.event_date <= local_date + timedelta(days=EVENT_AHEAD_DAYS):
            raise ValueError("The day it is expected must be from that day and within a year")
    return {k: v for k, v in values.items() if v not in (None, "", [], {})}


def _store(db: Session, entry: Entry, values: dict):
    for key, value in values.items():
        db.add(CapturedValue(entry_id=entry.id, key=key, value=json.dumps(value, ensure_ascii=False),
                             skipped=False, status="answered"))


def _screen(db: Session, account, entry: Entry, values: dict):
    """Her words are screened before anything else reads them, as in every journal."""
    from app.routers.entries import _screen_journal  # the shared journal screening

    words = [values.get(k) for k in (V_WHAT, V_SENSORY, V_WHY, V_SOUVENIR)]
    words += values.get(V_PEOPLE, []) + values.get(V_PLACES, [])
    text = "\n".join(w for w in words if w)
    if not text:
        return None
    result, _, _ = _screen_journal(db, account, entry, "savoring", text, history=[])
    return result


# ---------- writing ----------

def count_on(db: Session, account_id, day: date) -> int:
    return db.query(Entry).filter(
        Entry.account_id == account_id, Entry.journal_type == JOURNAL, Entry.status == "completed",
        Entry.local_date == day,
    ).count()


def positive_feelings_of(db: Session, account_id, source_id) -> list[str]:
    """A check-in's positive feelings, to start a moment opened from it. Its harder ones stay out."""
    source = db.query(Entry).filter(Entry.id == source_id, Entry.account_id == account_id).first()
    if source is None or source.journal_type != "check_in":
        raise Refused("bad_source", "A moment can only be opened from a check-in.")
    row = db.query(CapturedValue).filter(CapturedValue.entry_id == source.id, CapturedValue.key == "feelings").first()
    chosen = json.loads(row.value) if row and row.value else []
    return [f for f in chosen if f in POSITIVE_FEELINGS]


def create(db: Session, account, payload, terms: dict[str, str], now_utc: datetime | None = None):
    """A new moment, or the one already saved under the same client id. Returns (entry, crisis result)."""
    if payload.client_id is not None:
        same = db.query(Entry).filter(Entry.client_id == payload.client_id).first()
        if same is not None:
            if same.account_id != account.id:
                raise Refused("conflict", "That moment could not be saved.")
            return same, None

    today = slots.local_now(payload.tz_offset, now_utc).date()
    day = payload.local_date or today
    if not today - timedelta(days=BACKFILL_DAYS) <= day <= today:
        raise Refused("outside_window", "A moment can be dated today or yesterday.")
    # ponytail: the cap is counted, not locked; two moments sent in the same
    # instant could both get in. A soft cap tolerates that.
    if count_on(db, account.id, day) >= CAP:
        raise Refused("daily_cap", cap_message(day, today))
    if payload.source_entry_id is not None:
        positive_feelings_of(db, account.id, payload.source_entry_id)  # it must be her check-in

    values = validate(payload, terms, day)
    now = now_utc or datetime.utcnow()
    entry = Entry(
        account_id=account.id, journal_type=JOURNAL, status="completed", started_at=now, completed_at=now,
        client_id=payload.client_id, local_date=day, tz_offset=payload.tz_offset, timezone=payload.timezone,
        source_entry_id=payload.source_entry_id,
        event_date=payload.event_date if payload.type == ANTICIPATE else None,
    )
    db.add(entry)
    db.flush()
    _store(db, entry, {**values, V_MODE: payload.mode})
    # Saved before anything reads it (FR-ENT-003).
    db.commit()
    result = _screen(db, account, entry, values)
    db.commit()
    db.refresh(entry)
    return entry, result


def update(db: Session, account, entry: Entry, content, terms: dict[str, str]):
    """Her edit replaces what she put down; how it was captured and its follow-up stay."""
    values = validate(content, terms, entry.local_date or entry.started_at.date())
    db.query(CapturedValue).filter(
        CapturedValue.entry_id == entry.id, CapturedValue.key.in_(CONTENT_VALUES)
    ).delete(synchronize_session=False)
    _store(db, entry, values)
    entry.event_date = content.event_date if content.type == ANTICIPATE else None
    db.commit()
    result = _screen(db, account, entry, values)
    db.commit()
    db.refresh(entry)
    return entry, result


def set_actual(db: Session, entry: Entry, rating: int, today: date):
    """"How was it really?": only for a moment she looked forward to, once its day has come."""
    recorded = recorded_values(db, [entry]).get(entry.id, {})
    if recorded.get(V_TYPE) != ANTICIPATE or entry.event_date is None or entry.event_date > today:
        raise Refused("not_yet", "This can be added once the day has come.")
    db.query(CapturedValue).filter(CapturedValue.entry_id == entry.id, CapturedValue.key == V_ACTUAL).delete()
    _store(db, entry, {V_ACTUAL: rating})
    db.commit()


# ---------- what the app reads ----------

def moments(db: Session, account_id) -> list[Entry]:
    """Every finished moment, oldest first. A person keeps a few a day, so all of them is small."""
    return db.query(Entry).filter(
        Entry.account_id == account_id, Entry.journal_type == JOURNAL, Entry.status == "completed",
    ).order_by(Entry.completed_at).all()


def recorded_values(db: Session, entries: list[Entry]) -> dict:
    return insights._values(db, entries)


def day_of(entry: Entry, tz_offset: int) -> date:
    """Her day for a moment; one from before this module is dated by when it was finished."""
    if entry.local_date is not None:
        return entry.local_date
    offset = entry.tz_offset if entry.tz_offset is not None else tz_offset
    return insights.local_date(entry.completed_at or entry.started_at, offset)


def local_time(entry: Entry, tz_offset: int) -> datetime:
    offset = entry.tz_offset if entry.tz_offset is not None else tz_offset
    return (entry.completed_at or entry.started_at) + timedelta(minutes=offset)


def lift(rec: dict) -> int | None:
    """Mood after minus mood before, only when she gave both."""
    before, after = rec.get(V_MOOD_BEFORE), rec.get(V_MOOD_AFTER)
    return after - before if isinstance(before, int) and isinstance(after, int) else None


def describe(entry: Entry, rec: dict, terms: dict[str, str], tz_offset: int, today: date) -> dict:
    """One moment as the app shows it. Ids come with their names; her words as she wrote them."""
    def named(library: str, ids) -> list[dict]:
        return [{"id": i, "name": insights._label(library, i, terms)} for i in ids or []]

    ratings = rec.get(V_RATINGS) or {}
    kind = rec.get(V_TYPE, MOMENT)
    return {
        "id": str(entry.id),
        "type": kind,
        "mode": rec.get(V_MODE, QUICK),
        "local_date": day_of(entry, tz_offset).isoformat(),
        "created_at": local_time(entry, tz_offset).isoformat(timespec="minutes"),
        "event_date": entry.event_date.isoformat() if entry.event_date else None,
        "is_favorite": bool(entry.is_favorite),
        "source_entry_id": str(entry.source_entry_id) if entry.source_entry_id else None,
        "what_happened": rec.get(V_WHAT),
        "categories": named(CATEGORIES, rec.get(V_CATEGORIES)),
        "people": rec.get(V_PEOPLE) or [],
        "places": rec.get(V_PLACES) or [],
        "mood_before": rec.get(V_MOOD_BEFORE),
        "mood_after": rec.get(V_MOOD_AFTER),
        "mood_lift": lift(rec),
        "emotions": [{**e, "intensity": ratings.get(e["id"])} for e in named(FEELINGS, rec.get(V_FEELINGS))],
        "sensory_note": rec.get(V_SENSORY),
        "techniques": named(TECHNIQUES, rec.get(V_TECHNIQUES)),
        "why_good": rec.get(V_WHY),
        "souvenir_line": rec.get(V_SOUVENIR),
        "expected_rating": rec.get(V_EXPECTED),
        "actual_rating": rec.get(V_ACTUAL),
        "follow_up_due": (kind == ANTICIPATE and entry.event_date is not None and entry.event_date <= today
                          and rec.get(V_ACTUAL) is None),
    }


def described(db: Session, account_id, terms: dict[str, str], tz_offset: int, today: date) -> list[dict]:
    """Every moment, newest first."""
    entries = moments(db, account_id)
    recorded = recorded_values(db, entries)
    return [describe(e, recorded.get(e.id, {}), terms, tz_offset, today) for e in reversed(entries)]


def follow_ups(rows: list[dict], today: date) -> list[dict]:
    """Moments she looked forward to whose day has come, not yet told how they went (a month at most)."""
    return [r for r in rows if r["follow_up_due"]
            and date.fromisoformat(r["event_date"]) >= today - timedelta(days=FOLLOW_UP_DAYS)]


def memory_lane(rows: list[dict], today: date, account_id) -> list[dict]:
    """
    Two or three moments at least a week old, to live again: favourites first,
    then the ones that lifted her mood most. The same ones all week, new ones
    the next.
    """
    old = [r for r in rows if date.fromisoformat(r["local_date"]) <= today - timedelta(days=MEMORY_AGE_DAYS)]
    if len(old) < 2:
        return []
    favourites = [r for r in old if r["is_favorite"]]
    others = sorted((r for r in old if not r["is_favorite"]),
                    key=lambda r: -(r["mood_lift"] if r["mood_lift"] is not None else -99))
    pool = (favourites + others)[:MEMORY_POOL]
    week = today.isocalendar()
    return random.Random(f"{account_id}-{week.year}-{week.week}").sample(pool, min(MEMORY_SHOWN, len(pool)))


def _checkin_moods(db: Session, account_id, since: datetime) -> list[int]:
    entries = db.query(Entry).filter(
        Entry.account_id == account_id, Entry.journal_type == "check_in", Entry.status == "completed",
        Entry.completed_at >= since,
    ).all()
    recorded = recorded_values(db, entries)
    return [m for e in entries if isinstance(m := recorded.get(e.id, {}).get("mood"), int)]


def support(db: Session, account_id, rows: list[dict], now_utc: datetime | None = None) -> str | None:
    """
    None, "break" or "reach_out". Never a block: when the last LOW_RUN moments
    all ended very low, the app suggests a short break and someone to talk to;
    with a low week of check-ins too, it encourages professional support.
    """
    afters = [r["mood_after"] for r in rows if r["mood_after"] is not None][:LOW_RUN]
    if len(afters) < LOW_RUN or any(a > LOW_MOOD for a in afters):
        return None
    moods = _checkin_moods(db, account_id, (now_utc or datetime.utcnow()) - timedelta(days=CHECKIN_DAYS))
    if len(moods) >= CHECKIN_MIN and sum(moods) / len(moods) <= CHECKIN_LOW:
        return "reach_out"
    return "break"


# ---------- heatmap and streak ----------

def points_by_day(rows: list[dict]) -> dict[date, int]:
    out: dict[date, int] = {}
    for r in rows:
        day = date.fromisoformat(r["local_date"])
        out[day] = out.get(day, 0) + POINTS.get(r["mode"], 1)
    return out


# The level, the streak and the grid are shared with the other journals (core/streaks.py).
level, streaks = shared_streaks.level, shared_streaks.streaks


def heatmap(rows: list[dict], start: date, end: date, today: date) -> dict:
    return shared_streaks.heatmap(points_by_day(rows), start, end, today)
