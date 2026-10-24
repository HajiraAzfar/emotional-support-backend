"""
The Thought Journal's report: in the app (GET /reports/thought) and as section
B of the combined clinical report.

Only finished records count. An average shows only once MIN_RECORDS finished
records carry the values behind it — a record from before beliefs were asked
for counts towards the totals, never towards a belief average. Insights are
patterns ("tended to"), never causes, advice or a judgement of a thought;
recurring themes come from tag and pattern counts, never from reading her
words. The wording is in content/thought_report.json.
"""
import json
import logging
import uuid
from collections import Counter
from datetime import date, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from app.core import insights, savoring_report, thought as th
from app.models.entry import Entry

logger = logging.getLogger(__name__)

TEXT = json.loads(
    (Path(__file__).resolve().parent.parent / "content" / "thought_report.json").read_text(encoding="utf-8")
)

MIN_RECORDS = 5
TOP = 5
# Belief within this many points of where it started reads as "about the same".
BELIEF_SAME = 5
# Evidence against compared with none: groups of at least MIN_GROUP, a gap of GAP points.
MIN_GROUP, GAP = 3, 10
MIN_REPEATS = 3


def _mean(values) -> float | None:
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 1) if values else None


def _say(key: str, **values) -> str:
    return TEXT["insights"][key].format(**values)


def in_period(rows: list[dict], start: date, end: date) -> list[dict]:
    return [r for r in rows if start <= date.fromisoformat(r["local_date"]) <= end]


def _top(counter: Counter) -> list[dict]:
    return [{"name": n, "count": c} for n, c in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP]]


def high_checkins(db: Session, account_id, rows: list[dict]) -> set[str]:
    """The check-ins these records came from that were high in stress or low in mood."""
    ids = {uuid.UUID(r["source_entry_id"]) for r in rows if r["source_entry_id"]}
    if not ids:
        return set()
    entries = db.query(Entry).filter(Entry.account_id == account_id, Entry.id.in_(ids)).all()
    recorded = insights._values(db, entries)
    out = set()
    for e in entries:
        rec = recorded.get(e.id, {})
        anxiety, mood = rec.get("anxiety"), rec.get("mood")
        if (isinstance(anxiety, int) and anxiety >= th.HIGH_ANXIETY) or (isinstance(mood, int) and mood <= th.LOW_MOOD):
            out.add(str(e.id))
    return out


def build(rows: list[dict], start: date, end: date, high: set[str]) -> dict:
    """The report for one period, from described records (thought.describe), drafts included for the rate."""
    rows = in_period(rows, start, end)
    done = [r for r in rows if r["status"] == "completed"]
    beliefs = [r for r in done if r["belief_drop"] is not None]
    emotions = [r for r in done if r["emotion_drop"] is not None]
    balanced = [r for r in done if isinstance(r["balanced_belief"], int)]

    def enough(group, key):
        return _mean(r[key] for r in group) if len(group) >= MIN_RECORDS else None

    report = {
        "from": start.isoformat(), "to": end.isoformat(),
        "summary": {
            "completed": len(done),
            # ponytail: drafts older than a week are deleted, so the rate counts the drafts still kept.
            "started": len(rows),
            "completion_rate": round(len(done) / len(rows), 2) if rows else None,
            "with_belief": len(beliefs),
            "avg_belief_drop": enough(beliefs, "belief_drop"),
            "with_emotions": len(emotions),
            "avg_emotion_drop": enough(emotions, "emotion_drop"),
            "with_balanced_belief": len(balanced),
            "balanced_belief_avg": enough(balanced, "balanced_belief"),
            "linked_high_checkins": sum(1 for r in done if r["source_entry_id"] in high),
        },
        "traps": _top(Counter(t["name"] for r in done for t in r["thinking_traps"])),
        "tags": _top(Counter(t["name"] for r in done for t in r["situation_tags"])),
        # The reframes that went with the biggest drop in belief.
        "reframes": sorted((r for r in beliefs if r["balanced_thought"] and r["belief_drop"] > 0),
                           key=lambda r: -r["belief_drop"])[:3],
        "pattern": th.recurring(done, end),
    }
    rated = max(len(beliefs), len(emotions))
    if rated < MIN_RECORDS:
        needed = MIN_RECORDS - rated
        report["insights"] = None
        report["locked"] = {"needed": needed, "text": TEXT["locked"].format(n=needed, s="" if needed == 1 else "s")}
    else:
        report["insights"] = _insights(report, beliefs)
        report["locked"] = None
    return report


def _insights(report: dict, beliefs: list[dict]) -> list[str]:
    s = report["summary"]
    out = []
    if s["avg_belief_drop"] is not None:
        out.append(_say("belief_same") if abs(s["avg_belief_drop"]) < BELIEF_SAME
                   else _say("belief_drop", drop=s["avg_belief_drop"]) if s["avg_belief_drop"] > 0 else None)
    if s["avg_emotion_drop"] is not None and s["avg_emotion_drop"] >= BELIEF_SAME:
        out.append(_say("emotion_drop", drop=s["avg_emotion_drop"]))
    if report["traps"] and report["traps"][0]["count"] >= MIN_REPEATS:
        out.append(_say("trap", trap=report["traps"][0]["name"]))
    if report["tags"] and report["tags"][0]["count"] >= MIN_REPEATS:
        out.append(_say("tag", tag=report["tags"][0]["name"]))
    with_against = [r["belief_drop"] for r in beliefs if r["evidence_against"]]
    without = [r["belief_drop"] for r in beliefs if not r["evidence_against"]]
    if len(with_against) >= MIN_GROUP and len(without) >= MIN_GROUP and _mean(with_against) - _mean(without) >= GAP:
        out.append(_say("against", with_=_mean(with_against), without=_mean(without)))
    if s["balanced_belief_avg"] is not None:
        out.append(_say("balanced", belief=round(s["balanced_belief_avg"])))
    return [line for line in out if line]


# ---------- the combined clinical report and the shared events ----------

# What a record says, by the names the clinical report uses (thought.FIELDS maps the stored ids).
EXCERPT_FIELDS = ("local_date", "situation", "automatic_thought", "belief_before", "belief_after",
                  "balanced_thought", "balanced_belief")


def thought_section(db: Session, account_id, period, consent) -> dict:
    """
    Section B of the combined clinical report. Patterns only, from counts and
    ratings; her words only when consent.include_excerpts is true. Never raises:
    an empty period, or anything that goes wrong, comes back as empty.
    """
    empty = {"empty": True, "note": TEXT["empty_section"]}
    try:
        start, end = savoring_report._dates(period)
        rows = in_period(th.described(db, account_id, savoring_report._terms(db, account_id), 0, status=None),
                         start, end)
        if not any(r["status"] == "completed" for r in rows):
            return empty
        report = build(rows, start, end, high_checkins(db, account_id, rows))
        s = report["summary"]
        section = {
            "section": "B",
            "title": "Thought Journal",
            "period": {"from": start.isoformat(), "to": end.isoformat()},
            "completed": s["completed"],
            "completion_rate": s["completion_rate"],
            "records_with_belief_ratings": s["with_belief"],
            "avg_belief_drop": s["avg_belief_drop"],
            "avg_emotion_drop": s["avg_emotion_drop"],
            "balanced_belief_avg": s["balanced_belief_avg"],
            "top_thinking_patterns": report["traps"],
            "top_situation_tags": report["tags"],
            "records_linked_to_high_anxiety_checkins": s["linked_high_checkins"],
            "recurring_pattern": (
                {**report["pattern"], "text": TEXT["pattern"].format(tag=report["pattern"]["tag"],
                                                                    trap=report["pattern"]["trap"],
                                                                    n=report["pattern"]["records"])}
                if report["pattern"] else None
            ),
        }
        include = consent.get("include_excerpts") if isinstance(consent, dict) else getattr(consent, "include_excerpts", False)
        if include:
            chosen = report["reframes"] or [r for r in rows if r["status"] == "completed"][:3]
            section["excerpts"] = [{k: r[k] for k in EXCERPT_FIELDS} for r in chosen]
        return section
    except Exception:  # noqa: BLE001 — the composer must always get a section
        logger.exception("thought_section could not be built")  # no entry text in the log
        return {**empty, "note": "Thought Journal section could not be built"}


def journal_events(db: Session, account_id, start: date, end: date) -> list[dict]:
    """This journal's rows of journal_events: mood_signal is how much the feelings eased, on 0-10."""
    rows = in_period(th.described(db, account_id, {}, 0), start, end)
    return [{"user_id": str(account_id), "module": "thought", "entry_id": r["id"], "local_date": r["local_date"],
             "created_at": r["created_at"],
             "mood_signal": round(r["emotion_drop"] / 10, 1) if r["emotion_drop"] is not None else None}
            for r in rows]
