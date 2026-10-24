"""
Check-in reports: one finished day, week (Monday to Sunday) or month of the
three-a-day check-ins, ready for checkin_pdf.py to draw.

Only finished slot check-ins count. A period is reported once it is over on
her clock: a day at 05:00 the next morning, when its evening closes; a week on
the Monday after; a month on the 1st. A slot can only be filled in inside its
window and a finished one is never changed, so a report says the same thing
every time it is opened.

The conclusions are rule-based: a sentence appears only when the numbers
behind it are there (MIN_GROUP and the gaps below). A day only describes;
patterns need a week or a month. Nothing explains, predicts or diagnoses, and
PHQ-9 / GAD-7 appear only as an unlabelled line. The wording is in
content/checkin_report.json, for the clinical advisor.

ponytail: built on request, not stored; an issued-report history can sit on
top of build() when it is wanted.
"""
import calendar
import json
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core import assessments, capture, insights, slots
from app.models.entry import Entry

TEXT = json.loads(
    (Path(__file__).resolve().parent.parent / "content" / "checkin_report.json").read_text(encoding="utf-8")
)

DAILY, WEEKLY, MONTHLY = "daily", "weekly", "monthly"
KINDS = (DAILY, WEEKLY, MONTHLY)
NOUNS = {DAILY: "day", WEEKLY: "week", MONTHLY: "month"}

# A pattern needs at least this many check-ins (or days) on each side of it.
MIN_GROUP = 3
# The smallest differences worth a sentence: stress 0-10, mood 1-5.
STRESS_GAP, MOOD_GAP = 1.0, 0.5
DAY_STRESS_GAP = 2
# Mood against the period before: under this it is "about the same", under
# the second "a little".
MOOD_SAME, MOOD_SMALL = 0.3, 0.8
# A previous period is compared only with at least this many moods in it.
MIN_FOR_COMPARISON = insights.MIN_FOR_COMPARISON
# Day ratings (0-10) that make a better and a harder day.
BETTER_DAY, HARDER_DAY = 7, 4
# sleep_hours values below this are "under 6 hours".
SHORT_SLEEP = 6
# How many of the newest reports the list offers, and how many of her own
# sentences a report quotes per question.
LISTED = {DAILY: 30, WEEKLY: 12, MONTHLY: 12}
QUOTED = {WEEKLY: 10, MONTHLY: 15}
# How far back the weekly questions' line reaches, from the period's end.
QUESTIONS_LOOKBACK = {WEEKLY: timedelta(days=28), MONTHLY: timedelta(days=62)}
# A coping answer that is not a way of coping.
NO_COPING = "nothing"


class NotReady(Exception):
    """The period is not over yet on her clock."""


class Empty(Exception):
    """No finished check-in in the period."""


# ---------- periods ----------

def bounds(kind: str, start: date) -> tuple[date, date]:
    """First and last day of the period starting on `start` (a Monday for a week, the 1st for a month)."""
    if kind == DAILY:
        return start, start
    if kind == WEEKLY:
        if start.weekday() != 0:
            raise ValueError("A week starts on a Monday")
        return start, start + timedelta(days=6)
    if kind == MONTHLY:
        if start.day != 1:
            raise ValueError("A month starts on the 1st")
        return start, start.replace(day=calendar.monthrange(start.year, start.month)[1])
    raise ValueError(f"kind must be one of: {', '.join(KINDS)}")


def _previous(kind: str, start: date) -> date:
    if kind == WEEKLY:
        return start - timedelta(days=7)
    return (start - timedelta(days=1)).replace(day=1)


def title(kind: str, start: date, end: date) -> str:
    if kind == DAILY:
        return f"{start.strftime('%A')} {start.day} {start.strftime('%B %Y')}"
    if kind == WEEKLY:
        if start.month == end.month:
            return f"{start.day}–{end.day} {end.strftime('%B %Y')}"
        return f"{start.day} {start.strftime('%b')} – {end.day} {end.strftime('%b %Y')}"
    return start.strftime("%B %Y")


# ---------- small readers ----------

def _num(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _text(value) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _ids(value) -> list[str]:
    return [v for v in value if isinstance(v, str)] if isinstance(value, list) else []


def _mean(values) -> float | None:
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 1) if values else None


def _label(scale: str, value: int | None) -> str | None:
    if value is None:
        return None
    return next((o["label"] for o in capture.SCALES[scale] if o["value"] == value), str(value))


def _names(library: str, ids: list[str], terms: dict) -> list[str]:
    return [insights._label(library, i, terms) for i in ids]


def _join(items: list[str]) -> str:
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _say(key: str, **values) -> str:
    return TEXT["conclusions"][key].format(**values)


def _checkins(db: Session, account_id, start: date, end: date) -> list[Entry]:
    return db.query(Entry).filter(
        Entry.account_id == account_id, Entry.journal_type == "check_in", Entry.slot.isnot(None),
        Entry.status == "completed", Entry.local_date >= start, Entry.local_date <= end,
    ).order_by(Entry.local_date, Entry.started_at).all()


def _days(entries: list[Entry], values: dict) -> dict[date, dict[str, dict]]:
    """day → slot → what that check-in recorded."""
    out: dict[date, dict[str, dict]] = {}
    for entry in entries:
        out.setdefault(entry.local_date, {})[entry.slot] = values.get(entry.id, {})
    return out


def _day_mood(day: dict[str, dict]) -> float | None:
    return _mean(_num(rec.get("mood")) for rec in day.values())


def _day_activities(day: dict[str, dict]) -> set[str]:
    """What the day held: the afternoon's "so far" and the evening's "today included"."""
    return set(_ids(day.get(slots.AFTERNOON, {}).get("activities"))) | set(
        _ids(day.get(slots.EVENING, {}).get("day_activities")))


# ---------- one day ----------

def _daily(day: dict[str, dict], terms: dict) -> dict:
    rows = []
    for slot in slots.SLOTS:
        rec = day.get(slot)
        if rec is None:
            rows.append({"slot": slot, "done": False})
            continue
        ratings = rec.get("feeling_ratings") if isinstance(rec.get("feeling_ratings"), dict) else {}
        rows.append({
            "slot": slot, "done": True,
            "mood": _num(rec.get("mood")), "stress": _num(rec.get("anxiety")), "energy": _num(rec.get("energy")),
            "feelings": [{"name": insights._label("feelings", fid, terms), "strength": _num(ratings.get(fid))}
                         for fid in _ids(rec.get("feelings"))],
        })
    morning, afternoon, evening = (day.get(s, {}) for s in slots.SLOTS)
    coping = [c for c in _ids(afternoon.get("coping")) if c != NO_COPING]
    return {
        "slots": rows,
        "morning": {
            "sleep_hours": _label("sleep_hours", _num(morning.get("sleep_hours"))),
            "sleep_quality": _label("sleep_quality_5", _num(morning.get("sleep_quality"))),
            "intention": _text(morning.get("intention")),
            "hard_ahead": _names("triggers", _ids(morning.get("dread_triggers")), terms),
            "hard_note": _text(morning.get("dread_note")),
        } if morning else None,
        "afternoon": {
            "activities": _names("activities", _ids(afternoon.get("activities")), terms),
            "pleasure": _num(afternoon.get("pleasure")),
            "achievement": _num(afternoon.get("achievement")),
            "stress_event": _num(afternoon.get("stress_event")),
            "stress_about": _names("triggers", _ids(afternoon.get("stress_triggers")), terms),
            "stress_note": _text(afternoon.get("stress_note")),
            "coping": _names("coping", coping, terms),
        } if afternoon else None,
        "evening": {
            "day_rating": _num(evening.get("day_rating")),
            "day_rating_label": _label("day_rating_11", _num(evening.get("day_rating"))),
            "activities": _names("activities", _ids(evening.get("day_activities")), terms),
            "plan": _num(evening.get("plan_completed")),
            "plan_label": _label("plan_3", _num(evening.get("plan_completed"))),
            "helped": _text(evening.get("what_helped")),
            "hard": _text(evening.get("what_was_hard")),
            "win": _text(evening.get("win")),
            "bothering_thoughts": _label("yes_no", _num(evening.get("bothering_thoughts"))),
            "good_moment": _label("yes_no", _num(evening.get("good_moment"))),
            "faced_avoidance": _label("yes_no", _num(evening.get("faced_avoidance"))),
        } if evening else None,
    }


def _day_conclusions(report: dict) -> list[str]:
    """What the day was, in a few plain sentences. One day has too little in it for a pattern."""
    done = [r for r in report["slots"] if r["done"]]
    out = [_say("day_checkins", n=len(done), slots=_join([r["slot"] for r in done]))]

    moods = [(r["slot"], r["mood"]) for r in done if r["mood"] is not None]
    if len(moods) >= 2:
        (first_slot, first), (last_slot, last) = moods[0], moods[-1]
        if len({m for _, m in moods}) == 1:
            out.append(_say("day_mood_steady", label=_label("mood_5", first).lower()))
        elif first != last:
            out.append(_say("day_mood_moved", first=_label("mood_5", first).lower(), first_slot=first_slot,
                            last=_label("mood_5", last).lower(), last_slot=last_slot))

    stress = [(r["slot"], r["stress"]) for r in done if r["stress"] is not None]
    if len(stress) >= 2:
        high = max(stress, key=lambda s: s[1])
        low = min(stress, key=lambda s: s[1])
        if high[1] - low[1] >= DAY_STRESS_GAP:
            out.append(_say("day_stress_peak", high_slot=high[0], high=high[1], low_slot=low[0], low=low[1]))

    morning = report["morning"]
    if morning and morning["sleep_hours"]:
        hours = morning["sleep_hours"][0].lower() + morning["sleep_hours"][1:]
        out.append(_say("day_sleep", hours=hours, quality=morning["sleep_quality"].lower())
                   if morning["sleep_quality"] else _say("day_sleep_hours", hours=hours))

    rated = [(f["strength"], f["name"], r["slot"]) for r in done for f in r["feelings"] if f["strength"] is not None]
    if rated:
        value, name, slot = max(rated, key=lambda f: f[0])
        out.append(_say("day_strongest_feeling", feeling=name.lower(), value=value, slot=slot))

    afternoon = report["afternoon"]
    if afternoon and afternoon["stress_event"] == 1:
        about = f" ({_join([a.lower() for a in afternoon['stress_about']])})" if afternoon["stress_about"] else ""
        out.append(_say("day_stress_event", about=about))
        if afternoon["coping"]:
            out.append(_say("day_coped", coping=_join(afternoon["coping"])))

    evening = report["evening"]
    if evening and evening["day_rating"] is not None:
        out.append(_say("day_rating", value=evening["day_rating"], label=evening["day_rating_label"].lower()))
    if evening and evening["plan"] is not None:
        out.append(_say(f"day_plan_{evening['plan']}"))
    return out


# ---------- a week or a month ----------

def _counted(rows: Counter, library: str, terms: dict, exclude: str | None = None) -> list[dict]:
    return [{"id": i, "name": insights._label(library, i, terms), "count": n}
            for i, n in sorted(rows.items(), key=lambda kv: (-kv[1], kv[0])) if i != exclude]


def _period(kind: str, start: date, end: date, days: dict, prev_days: dict, terms: dict) -> dict:
    every_day = [start + timedelta(days=n) for n in range((end - start).days + 1)]
    recs = [(d, slot, rec) for d in sorted(days) for slot, rec in days[d].items()]

    series = []
    for d in every_day:
        day = days.get(d, {})
        afternoon, evening, morning = (day.get(s, {}) for s in (slots.AFTERNOON, slots.EVENING, slots.MORNING))
        series.append({
            "date": d.isoformat(),
            "mood": {slot: _num(day.get(slot, {}).get("mood")) for slot in slots.SLOTS},
            "day_mood": _day_mood(day),
            "stress": _mean(_num(rec.get("anxiety")) for rec in day.values()),
            "pleasure": _num(afternoon.get("pleasure")),
            "achievement": _num(afternoon.get("achievement")),
            "day_rating": _num(evening.get("day_rating")),
            "sleep_hours": _num(morning.get("sleep_hours")),
        })

    by_slot = []
    for slot in slots.SLOTS:
        mine = [rec for _, s, rec in recs if s == slot]
        moods = [_num(r.get("mood")) for r in mine if _num(r.get("mood")) is not None]
        stress = [_num(r.get("anxiety")) for r in mine if _num(r.get("anxiety")) is not None]
        by_slot.append({
            "slot": slot, "check_ins": len(mine),
            "mood": _mean(moods), "mood_n": len(moods),
            "stress": _mean(stress), "stress_n": len(stress),
            "energy": _mean(_num(r.get("energy")) for r in mine),
        })

    short, enough = [], []
    for row in series:
        if row["sleep_hours"] is not None and row["day_mood"] is not None:
            (short if row["sleep_hours"] < SHORT_SLEEP else enough).append(row["day_mood"])

    feelings, triggers, coping, stress_triggers = Counter(), Counter(), Counter(), Counter()
    for _, slot, rec in recs:
        feelings.update(set(_ids(rec.get("feelings"))))
        triggers.update(set(_ids(rec.get("dread_triggers"))) | set(_ids(rec.get("stress_triggers"))))
        coping.update(set(_ids(rec.get("coping"))))
        stress_triggers.update(set(_ids(rec.get("stress_triggers"))))
    activities = Counter(a for d in days for a in _day_activities(days[d]))

    def group(test) -> dict:
        chosen = [d for d in days if (r := _num(days[d].get(slots.EVENING, {}).get("day_rating"))) is not None and test(r)]
        held = Counter(a for d in chosen for a in _day_activities(days[d]))
        return {"days": len(chosen), "activities": [{"name": row["name"], "days": row["count"]}
                                                    for row in _counted(held, "activities", terms)[:5]]}

    plans = Counter(_num(days[d].get(slots.EVENING, {}).get("plan_completed")) for d in days)
    plans.pop(None, None)

    quoted = QUOTED[kind]
    words = {
        key: [{"date": d.isoformat(), "text": t} for d in sorted(days)
              if (t := _text(days[d].get(slots.EVENING, {}).get(value_id)))][:quoted]
        for key, value_id in (("wins", "win"), ("helped", "what_helped"), ("hard", "what_was_hard"))
    }

    prev_recs = [rec for d in prev_days for rec in prev_days[d].values()]
    prev_moods = [_num(r.get("mood")) for r in prev_recs if _num(r.get("mood")) is not None]
    moods = [_num(r.get("mood")) for _, _, r in recs if _num(r.get("mood")) is not None]
    stress_all = [_num(r.get("anxiety")) for _, _, r in recs if _num(r.get("anxiety")) is not None]
    prev_stress = [_num(r.get("anxiety")) for r in prev_recs if _num(r.get("anxiety")) is not None]

    out = {
        "glance": {
            "check_ins": len(recs), "possible": 3 * len(every_day),
            "days_with": len(days), "days": len(every_day),
            "by_slot": {slot: sum(1 for _, s, _ in recs if s == slot) for slot in slots.SLOTS},
        },
        "series": series,
        "by_slot": by_slot,
        "sleep_mood": {"short": _mean(short), "short_days": len(short), "enough": _mean(enough), "enough_days": len(enough)},
        "feelings": _counted(feelings, "feelings", terms),
        "triggers": _counted(triggers, "triggers", terms),
        "stress_about": _counted(stress_triggers, "triggers", terms),
        "stress_days": sum(1 for d in days if _num(days[d].get(slots.AFTERNOON, {}).get("stress_event")) == 1),
        "coping": _counted(coping, "coping", terms, exclude=NO_COPING),
        "activities": [{"name": row["name"], "days": row["count"]} for row in _counted(activities, "activities", terms)],
        "better_days": group(lambda r: r >= BETTER_DAY),
        "harder_days": group(lambda r: r <= HARDER_DAY),
        "plan": {"yes": plans[2], "partly": plans[1], "not_really": plans[0]},
        "words": words,
        "compare": {
            "mood": _mean(moods), "mood_n": len(moods),
            "previous_mood": _mean(prev_moods), "previous_mood_n": len(prev_moods),
            "stress": _mean(stress_all), "previous_stress": _mean(prev_stress), "previous_stress_n": len(prev_stress),
        },
    }
    if kind == MONTHLY:
        weeks: dict[date, list[str]] = {}
        for d in every_day:
            weeks.setdefault(d - timedelta(days=d.weekday()), []).append(d.isoformat())
        by_date = {row["date"]: row for row in series}
        out["weeks"] = [{
            "start": dates[0], "end": dates[-1],
            "mood": _mean(m for d in dates for m in by_date[d]["mood"].values()),
            "stress": _mean(by_date[d]["stress"] for d in dates),
        } for dates in weeks.values()]
    return out


def _day_name(kind: str, iso: str) -> str:
    d = date.fromisoformat(iso)
    return d.strftime("%A") if kind == WEEKLY else f"{d.strftime('%a')} {d.day} {d.strftime('%b')}"


def _period_conclusions(kind: str, report: dict) -> list[str]:
    noun = NOUNS[kind]
    glance = report["glance"]
    out = [_say("checkins", n=glance["check_ins"], possible=glance["possible"], days=glance["days_with"],
                total_days=glance["days"])]
    patterns = []

    stressed = [row for row in report["by_slot"] if row["stress_n"] >= MIN_GROUP]
    if len(stressed) >= 2:
        low = min(stressed, key=lambda r: r["stress"])
        high = max(stressed, key=lambda r: r["stress"])
        if high["stress"] - low["stress"] >= STRESS_GAP:
            patterns.append(_say("calmest_slot", slot=low["slot"].capitalize(), low=low["stress"],
                                 high=high["stress"], high_slot=high["slot"]))

    moody = [row for row in report["by_slot"] if row["mood_n"] >= MIN_GROUP]
    if len(moody) >= 2:
        best = max(moody, key=lambda r: r["mood"])
        worst = min(moody, key=lambda r: r["mood"])
        if best["mood"] - worst["mood"] >= MOOD_GAP:
            patterns.append(_say("best_mood_slot", slot=best["slot"], best=best["mood"],
                                 worst=worst["mood"], worst_slot=worst["slot"]))

    sleep = report["sleep_mood"]
    if (sleep["short_days"] >= MIN_GROUP and sleep["enough_days"] >= MIN_GROUP
            and abs(sleep["enough"] - sleep["short"]) >= MOOD_GAP):
        patterns.append(_say("sleep_mood", short=sleep["short"], enough=sleep["enough"]))

    better = report["better_days"]
    if better["days"] >= 2 and better["activities"] and better["activities"][0]["days"] >= 2:
        top = better["activities"][0]
        patterns.append(_say("better_days_activity", activity=top["name"], n=top["days"], m=better["days"]))

    if report["stress_days"] >= 2:
        about = _say("stress_events_about", trigger=report["stress_about"][0]["name"]) \
            if report["stress_about"] and report["stress_about"][0]["count"] >= 2 else ""
        patterns.append(_say("stress_events", n=report["stress_days"], about=about))

    if report["coping"] and report["coping"][0]["count"] >= 2:
        patterns.append(_say("coping_top", coping=report["coping"][0]["name"], n=report["coping"][0]["count"]))

    if report["feelings"] and report["feelings"][0]["count"] >= 2:
        patterns.append(_say("top_feeling", feeling=report["feelings"][0]["name"], n=report["feelings"][0]["count"]))

    rated = [row for row in report["series"] if row["day_rating"] is not None]
    if len(rated) >= 2:
        best = max(rated, key=lambda r: r["day_rating"])
        hardest = min(rated, key=lambda r: r["day_rating"])
        if best["day_rating"] != hardest["day_rating"]:
            patterns.append(_say("best_day", best=_day_name(kind, best["date"]), best_value=best["day_rating"],
                                 hardest=_day_name(kind, hardest["date"]), hardest_value=hardest["day_rating"]))

    plan = report["plan"]
    evenings = plan["yes"] + plan["partly"] + plan["not_really"]
    if evenings >= MIN_GROUP:
        patterns.append(_say("plan", n=plan["yes"] + plan["partly"], m=evenings))

    compare = report["compare"]
    if compare["mood_n"] >= MIN_FOR_COMPARISON and compare["previous_mood_n"] >= MIN_FOR_COMPARISON:
        change = compare["mood"] - compare["previous_mood"]
        key = ("mood_same" if abs(change) < MOOD_SAME
               else ("mood_up_small" if change > 0 else "mood_down_small") if abs(change) < MOOD_SMALL
               else ("mood_up" if change > 0 else "mood_down"))
        patterns.append(_say(key, period=noun, now=compare["mood"], before=compare["previous_mood"]))
    if (compare["stress"] is not None and compare["previous_stress_n"] >= MIN_FOR_COMPARISON
            and abs(compare["stress"] - compare["previous_stress"]) >= STRESS_GAP):
        key = "stress_up" if compare["stress"] > compare["previous_stress"] else "stress_down"
        patterns.append(_say(key, period=noun, now=compare["stress"], before=compare["previous_stress"]))

    return out + (patterns or [_say("not_enough", period=noun)])


# ---------- what the app lists, and one report ----------

def listing(db: Session, account_id, tz_offset: int, now_utc: datetime | None = None) -> dict:
    """
    The reports she can open: every finished day, week and month with at least
    one finished check-in, newest first. `latest_daily` is the day before the
    one she is in now — the report the app shows on top until today is over —
    even when it had no check-in (check_ins is 0 then).
    """
    moment = slots.local_now(tz_offset, now_utc)
    counts = dict(
        db.query(Entry.local_date, func.count(Entry.id))
        .filter(Entry.account_id == account_id, Entry.journal_type == "check_in", Entry.slot.isnot(None),
                Entry.status == "completed")
        .group_by(Entry.local_date)
        .all()
    )

    def item(kind: str, start: date, n: int) -> dict:
        first, last = bounds(kind, start)
        return {"kind": kind, "start": first.isoformat(), "end": last.isoformat(),
                "title": title(kind, first, last), "check_ins": n}

    weeks, months = Counter(), Counter()
    for day, n in counts.items():
        weeks[day - timedelta(days=day.weekday())] += n
        months[day.replace(day=1)] += n

    def finished(kind: str, rows: dict) -> list[dict]:
        return [item(kind, start, n) for start, n in sorted(rows.items(), reverse=True)
                if slots.day_over(bounds(kind, start)[1], moment)][:LISTED[kind]]

    yesterday = slots.slot_at(moment)[1] - timedelta(days=1)
    return {
        "latest_daily": item(DAILY, yesterday, counts.get(yesterday, 0)),
        "daily": finished(DAILY, counts),
        "weekly": finished(WEEKLY, weeks),
        "monthly": finished(MONTHLY, months),
    }


def build(db: Session, account_id, kind: str, start: date, terms: dict, tz_offset: int,
          now_utc: datetime | None = None) -> dict:
    """One report. ValueError for a start that does not begin a period, NotReady, Empty."""
    start, end = bounds(kind, start)
    if not slots.day_over(end, slots.local_now(tz_offset, now_utc)):
        raise NotReady
    entries = _checkins(db, account_id, start, end)
    if not entries:
        raise Empty
    days = _days(entries, insights._values(db, entries))
    report = {"kind": kind, "start": start.isoformat(), "end": end.isoformat(),
              "title": title(kind, start, end), "period": NOUNS[kind], "check_ins": len(entries)}

    if kind == DAILY:
        report.update(_daily(days[start], terms))
        report["conclusions"] = _day_conclusions(report)
        return report

    prev_start = _previous(kind, start)
    prev_entries = _checkins(db, account_id, prev_start, bounds(kind, prev_start)[1])
    report.update(_period(kind, start, end, days, _days(prev_entries, insights._values(db, prev_entries)), terms))
    # The weekly questions, as stored (UTC), up to the moment the period closed.
    until = slots.window(end, slots.EVENING)[1] - timedelta(minutes=tz_offset)
    report["questions"] = {
        name: assessments.trend(db, account_id, name, until - QUESTIONS_LOOKBACK[kind], until)["points"]
        for name in (assessments.PHQ9, assessments.GAD7)
    }
    report["conclusions"] = _period_conclusions(kind, report)
    return report
