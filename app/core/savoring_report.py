"""
The Saving Journal's report: in the app (GET /reports/savoring), as a CSV she
exports, and as section D of the combined clinical report.

Counts use every moment; trends (mood lift, techniques, categories, the
insights) count at most TREND_PER_DAY moments a day, so a day synced twice or
written in a rush cannot outweigh the rest. Insights appear only from
MIN_FOR_INSIGHTS moments, each with its own minimum behind it, and are worded
as patterns ("tended to"), never as causes. The wording is in
content/savoring_report.json, for the clinical advisor.

The clinical section carries no streak, only how many days had a moment.
"""
import csv
import io
import json
import logging
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from statistics import median

from sqlalchemy.orm import Session

from app.core import capture, insights, savoring as sv
from app.models.entry import Entry
from app.models.user_term import UserTerm

logger = logging.getLogger(__name__)

TEXT = json.loads(
    (Path(__file__).resolve().parent.parent / "content" / "savoring_report.json").read_text(encoding="utf-8")
)

MIN_FOR_INSIGHTS = 7
TREND_PER_DAY = 5
MIN_TECHNIQUE_USES = 3
MIN_CATEGORY, MIN_ANTICIPATED, MIN_LIFTS, MIN_WEEKS, MIN_REPEATS = 3, 3, 5, 4, 3
LIFT_UP, GAP, ANTICIPATION_GAP, WEEK_GAP = 0.5, 0.5, 1.0, 0.3
TOP = 5
# Parts of her day, by the hour a moment was saved; the rest is night.
PARTS = (("morning", 5, 12), ("afternoon", 12, 17), ("evening", 17, 21))
TYPE_NAMES = {sv.MOMENT: "Just happened", sv.REMINISCE: "Remembered", sv.ANTICIPATE: "Looked forward to"}


def _mean(values) -> float | None:
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 1) if values else None


def _say(key: str, **values) -> str:
    return TEXT["insights"][key].format(**values)


def in_period(rows: list[dict], start: date, end: date) -> list[dict]:
    return [r for r in rows if start <= date.fromisoformat(r["local_date"]) <= end]


def trend_rows(rows: list[dict]) -> list[dict]:
    """The first TREND_PER_DAY moments of each day, in the order they were saved."""
    kept, per_day = [], Counter()
    for r in sorted(rows, key=lambda r: r["created_at"]):
        per_day[r["local_date"]] += 1
        if per_day[r["local_date"]] <= TREND_PER_DAY:
            kept.append(r)
    return kept


def _top(counter: Counter) -> list[dict]:
    return [{"name": name, "count": n} for name, n in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP]]


def _emotions(rows: list[dict]) -> list[dict]:
    """Library feelings by name; her own words together, so one-off phrases do not crowd the list."""
    counted = Counter()
    for r in rows:
        for e in r["emotions"]:
            counted[e["name"] if e["id"] in capture.LIBRARY_NAMES[sv.FEELINGS] else TEXT["other_words"]] += 1
    return _top(counted)


def _tags(rows: list[dict], key: str) -> list[dict]:
    """Who or where, counted regardless of case, shown as she first wrote them."""
    counted, shown = Counter(), {}
    for r in rows:
        for tag in r[key]:
            counted[tag.casefold()] += 1
            shown.setdefault(tag.casefold(), tag)
    return [{"name": shown[k], "count": n} for k, n in sorted(counted.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP]]


def _part(created_at: str) -> str:
    hour = datetime.fromisoformat(created_at).hour
    return next((name for name, start, end in PARTS if start <= hour < end), "night")


def _lift_by(rows: list[dict], key: str) -> dict[str, list[int]]:
    """Item name → the lifts of the moments it was part of (moments with both moods only)."""
    out: dict[str, list[int]] = {}
    for r in rows:
        if r["mood_lift"] is not None:
            for item in r[key]:
                out.setdefault(item["name"], []).append(r["mood_lift"])
    return out


def checkin_moods(db: Session, account_id, start: date, end: date, tz_offset: int) -> list[tuple[date, int]]:
    """Every check-in mood (1-5) in the period, with its day."""
    entries = db.query(Entry).filter(
        Entry.account_id == account_id, Entry.journal_type == "check_in", Entry.status == "completed",
    ).all()
    recorded = insights._values(db, entries)
    out = []
    for e in entries:
        mood = recorded.get(e.id, {}).get("mood")
        day = sv.day_of(e, tz_offset)
        if isinstance(mood, int) and start <= day <= end:
            out.append((day, mood))
    return out


def _weeks(rows: list[dict], moods: list[tuple[date, int]], start: date, end: date) -> list[dict]:
    first = start - timedelta(days=start.weekday())
    out = []
    while first <= end:
        last = first + timedelta(days=6)
        days = {r["local_date"] for r in rows if first <= date.fromisoformat(r["local_date"]) <= last}
        out.append({"week_start": first.isoformat(), "savoring_days": len(days),
                    "checkin_mood": _mean(m for d, m in moods if first <= d <= last)})
        first += timedelta(days=7)
    return out


def build(rows: list[dict], start: date, end: date, moods: list[tuple[date, int]]) -> dict:
    """The report for one period, from described moments (savoring.describe) and check-in moods."""
    rows = in_period(rows, start, end)
    trend = trend_rows(rows)
    lifts = [r["mood_lift"] for r in trend if r["mood_lift"] is not None]
    days = {r["local_date"] for r in rows}

    techniques = sorted(
        ({"name": name, "uses": len(values), "avg_lift": _mean(values)}
         for name, values in _lift_by(trend, "techniques").items() if len(values) >= MIN_TECHNIQUE_USES),
        key=lambda t: -t["avg_lift"],
    )
    anticipated = [r for r in trend if r["expected_rating"] is not None and r["actual_rating"] is not None]
    anticipation = {
        "expected": _mean(r["expected_rating"] for r in anticipated),
        "actual": _mean(r["actual_rating"] for r in anticipated),
        "entries": len(anticipated),
    } if anticipated else None
    favourites = [r for r in rows if r["is_favorite"]]
    lifted = sorted((r for r in rows if (r["mood_lift"] or 0) > 0), key=lambda r: -r["mood_lift"])

    report = {
        "from": start.isoformat(), "to": end.isoformat(),
        "summary": {
            "days_with_entries": len(days),
            "entries_total": len(rows),
            "avg_per_active_day": round(len(rows) / len(days), 1) if days else 0,
            "avg_mood_lift": _mean(lifts),
            "lift_entries": len(lifts),
            "types": {kind: sum(1 for r in rows if r["type"] == kind) for kind in sv.TYPES},
        },
        "emotions": _emotions(rows),
        "categories": _top(Counter(c["name"] for r in rows for c in r["categories"])),
        "people": _tags(rows, "people"),
        "places": _tags(rows, "places"),
        "time_of_day": {part: sum(1 for r in rows if _part(r["created_at"]) == part)
                        for part in ("morning", "afternoon", "evening", "night")},
        "techniques": techniques,
        "anticipation": anticipation,
        "weeks": _weeks(rows, moods, start, end),
        "highlights": (favourites[:3] if favourites else lifted[:3]),
    }
    if len(rows) < MIN_FOR_INSIGHTS:
        needed = MIN_FOR_INSIGHTS - len(rows)
        report["insights"] = None
        report["locked"] = {"needed": needed, "text": TEXT["locked"].format(n=needed, s="" if needed == 1 else "s")}
    else:
        report["insights"] = _insights(report, trend, lifts)
        report["locked"] = None
    return report


def _insights(report: dict, trend: list[dict], lifts: list[int]) -> list[str]:
    out = []
    overall = _mean(lifts)
    if len(lifts) >= MIN_LIFTS:
        if overall >= LIFT_UP:
            out.append(_say("lift_up", lift=overall))
        elif abs(overall) < LIFT_UP:
            out.append(_say("lift_same"))

    techniques = report["techniques"]
    if techniques and overall is not None and techniques[0]["avg_lift"] - overall >= GAP:
        out.append(_say("technique", technique=techniques[0]["name"], lift=techniques[0]["avg_lift"],
                        overall=overall))

    categories = {name: _mean(v) for name, v in _lift_by(trend, "categories").items() if len(v) >= MIN_CATEGORY}
    if categories and overall is not None:
        best = max(categories, key=categories.get)
        if categories[best] - overall >= GAP:
            out.append(_say("category", category=best))

    anticipation = report["anticipation"]
    if anticipation and anticipation["entries"] >= MIN_ANTICIPATED:
        change = anticipation["actual"] - anticipation["expected"]
        key = ("anticipation_better" if change >= ANTICIPATION_GAP
               else "anticipation_less" if change <= -ANTICIPATION_GAP else "anticipation_same")
        out.append(_say(key, actual=anticipation["actual"], expected=anticipation["expected"]))

    weeks = [w for w in report["weeks"] if w["checkin_mood"] is not None]
    if len(weeks) >= MIN_WEEKS:
        middle = median(w["savoring_days"] for w in weeks)
        more = [w["checkin_mood"] for w in weeks if w["savoring_days"] > middle]
        fewer = [w["checkin_mood"] for w in weeks if w["savoring_days"] <= middle]
        if more and fewer and abs(_mean(more) - _mean(fewer)) >= WEEK_GAP:
            out.append(_say("weeks_higher" if _mean(more) > _mean(fewer) else "weeks_lower",
                            more=_mean(more), fewer=_mean(fewer)))

    parts = sorted(report["time_of_day"].items(), key=lambda kv: -kv[1])
    if parts[0][1] >= MIN_REPEATS and parts[0][1] > parts[1][1]:
        out.append(_say("time_of_day", part=parts[0][0]))

    named = [e for e in report["emotions"] if e["name"] != TEXT["other_words"]]
    if named and named[0]["count"] >= MIN_REPEATS:
        out.append(_say("emotion", emotion=named[0]["name"]))
    return out


# ---------- the combined clinical report, the shared events, the CSV ----------

def _dates(period) -> tuple[date, date]:
    """(start, end), {"start", "end"} or an object with .start and .end; dates or ISO strings."""
    if isinstance(period, dict):
        start, end = period["start"], period["end"]
    elif isinstance(period, (tuple, list)):
        start, end = period
    else:
        start, end = period.start, period.end
    as_date = lambda d: d if isinstance(d, date) else date.fromisoformat(str(d))  # noqa: E731
    return as_date(start), as_date(end)


def _terms(db: Session, account_id) -> dict[str, str]:
    return {t.item_id: t.name for t in db.query(UserTerm).filter(UserTerm.account_id == account_id).all()}


def savoring_section(db: Session, account_id, period, consent) -> dict:
    """
    Section D of the combined clinical report (the composer, consent screen and
    template are built elsewhere). Patterns only: no streaks, no diagnoses, no
    causes; her own words only when consent.include_excerpts is true. Never
    raises: an empty period, or anything that goes wrong, comes back as empty.
    """
    empty = {"empty": True, "note": TEXT["empty_section"]}
    try:
        start, end = _dates(period)
        today = datetime.utcnow().date()
        rows = in_period(sv.described(db, account_id, _terms(db, account_id), 0, today), start, end)
        if not rows:
            return empty
        report = build(rows, start, end, [])
        summary = report["summary"]
        section = {
            "section": "D",
            "title": "Savoring",
            "period": {"from": start.isoformat(), "to": end.isoformat()},
            "days_with_entries": summary["days_with_entries"],
            "entries_total": summary["entries_total"],
            "avg_mood_lift": summary["avg_mood_lift"],
            "top_emotions": report["emotions"],
            "top_techniques_by_lift": report["techniques"],
            "anticipation_vs_reality": report["anticipation"],
            "adherence": {"days_active": summary["days_with_entries"], "days_in_period": (end - start).days + 1},
        }
        include = consent.get("include_excerpts") if isinstance(consent, dict) else getattr(consent, "include_excerpts", False)
        if include:
            section["excerpts"] = [
                {"date": r["local_date"], "type": TYPE_NAMES[r["type"]], "what_happened": r["what_happened"],
                 "souvenir_line": r["souvenir_line"]}
                for r in report["highlights"]
            ]
        return section
    except Exception:  # noqa: BLE001 — the composer must always get a section
        logger.exception("savoring_section could not be built")  # no entry text in the log
        return {**empty, "note": "Savoring section could not be built"}


def journal_events(db: Session, account_id, start: date, end: date) -> list[dict]:
    """This journal's rows of the shared journal_events shape (core/journal_events.py)."""
    rows = in_period(sv.described(db, account_id, {}, 0, datetime.utcnow().date()), start, end)
    return [{"user_id": str(account_id), "module": "savoring", "entry_id": r["id"], "local_date": r["local_date"],
             "created_at": r["created_at"], "mood_signal": r["mood_lift"]} for r in rows]


BASE_COLUMNS = ["date", "time", "type", "mode", "categories", "emotions", "techniques", "mood_before",
                "mood_after", "mood_lift", "expected_rating", "actual_rating", "event_date", "favorite"]
NOTE_COLUMNS = ["what_happened", "people", "places", "sensory_note", "why_good", "souvenir_line"]


def _cell(value):
    """A cell a spreadsheet cannot run as a formula."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return "" if value is None else value


def to_csv(rows: list[dict], include_notes: bool) -> str:
    """One row per moment, oldest first; feelings and techniques joined. Her words only when she chose to."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(BASE_COLUMNS + (NOTE_COLUMNS if include_notes else []))
    for r in sorted(rows, key=lambda r: r["created_at"]):
        line = [
            r["local_date"], r["created_at"][11:16], TYPE_NAMES[r["type"]], r["mode"],
            "; ".join(c["name"] for c in r["categories"]),
            "; ".join(e["name"] + (f" {e['intensity']}" if e["intensity"] is not None else "") for e in r["emotions"]),
            "; ".join(t["name"] for t in r["techniques"]),
            r["mood_before"], r["mood_after"], r["mood_lift"], r["expected_rating"], r["actual_rating"],
            r["event_date"], "yes" if r["is_favorite"] else "no",
        ]
        if include_notes:
            line += [r["what_happened"], "; ".join(r["people"]), "; ".join(r["places"]), r["sensory_note"],
                     r["why_good"], r["souvenir_line"]]
        writer.writerow([_cell(v) for v in line])
    return out.getvalue()
