"""
The Exposure Journal's report: in the app (GET /reports/exposure), as section C
of the combined clinical report, and her own export (ladders, steps and
exposures, as JSON).

Only exposures done fully or in part count towards averages, streaks and the
heatmap; skipped ones are counted on their own and named neutrally. Averages
show from MIN_DONE such exposures carrying the values behind them; records
from before (0-10) are read ten times over. Patterns only, never a cause, a
diagnosis or advice; the wording is in content/exposure_report.json.
"""
import json
import logging
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from app.core import exposure as ex, savoring_report

logger = logging.getLogger(__name__)

TEXT = json.loads(
    (Path(__file__).resolve().parent.parent / "content" / "exposure_report.json").read_text(encoding="utf-8")
)

MIN_DONE = 3
# How often the feared outcome happened: partly counts as half.
OUTCOME_SCORE = {"yes": 100, "partly": 50, "no": 0}
# Within this many points, the outcome happened "about as often" as predicted.
OUTCOME_SAME = 10
# Repeats: a peak that came down at least this far reads as coming down.
REPEAT_DROP = 5
TOP = 5


def _when(r: dict) -> tuple:
    """Oldest first: by her day, then by the time it was saved."""
    return r["local_date"], r["created_at"]


def _mean(values) -> float | None:
    values = [v for v in values if v is not None]
    return round(sum(values) / len(values), 1) if values else None


def _say(key: str, **values) -> str:
    return TEXT["insights"][key].format(**values)


def in_period(rows: list[dict], start: date, end: date) -> list[dict]:
    return [r for r in rows if start <= date.fromisoformat(r["local_date"]) <= end]


def _top(counter: Counter) -> list[dict]:
    return [{"name": n, "count": c} for n, c in sorted(counter.items(), key=lambda kv: (-kv[1], kv[0]))[:TOP]]


def _weeks(done: list[dict], start: date, end: date) -> list[dict]:
    first = start - timedelta(days=start.weekday())
    out = []
    while first <= end:
        last = first + timedelta(days=6)
        out.append({"week_start": first.isoformat(),
                    "exposures": sum(1 for r in done if first <= date.fromisoformat(r["local_date"]) <= last)})
        first += timedelta(days=7)
    return out


def build(rows: list[dict], ladders: list[dict], start: date, end: date) -> dict:
    """The report for one period, from described exposures (exposure.describe) and her ladders."""
    rows = in_period(rows, start, end)
    done = [r for r in rows if r["status"] in ex.COUNTED]
    completed = [r for r in done if r["status"] == ex.COMPLETED]
    skipped = [r for r in rows if r["status"] == ex.SKIPPED]

    def enough(group, key):
        values = [r[key] for r in group if r[key] is not None]
        return _mean(values) if len(values) >= MIN_DONE else None

    likely = [r for r in done if r["predicted_likelihood"] is not None and r["outcome_occurred"]]
    by_step: dict[str, list[dict]] = {}
    for r in sorted(done, key=_when):
        if r["ladder_item_id"]:
            by_step.setdefault(r["ladder_item_id"], []).append(r)
    repeats = []
    for attempts in by_step.values():
        peaks = [a["peak"] for a in attempts if a["peak"] is not None]
        if len(attempts) >= 2 and len(peaks) >= 2:
            step = attempts[0]["step"] or {}
            repeats.append({"step": step.get("description"), "fear": step.get("fear_name"), "attempts": len(attempts),
                            "first_peak": peaks[0], "last_peak": peaks[-1],
                            "first_predicted": attempts[0]["predicted_anxiety"],
                            "last_predicted": attempts[-1]["predicted_anxiety"]})
    highest = max((r["step"] for r in completed if r["step"]), key=lambda s: s["expected_suds"], default=None)
    done_steps = {r["ladder_item_id"] for r in completed if r["ladder_item_id"]}
    in_progress = sum(
        1 for ladder in ladders if not ladder["archived"]
        and any(s["attempts"] for s in ladder["steps"]) and any(s["id"] not in done_steps for s in ladder["steps"])
    )

    report = {
        "from": start.isoformat(), "to": end.isoformat(),
        "summary": {
            "done": len(done), "completed": len(completed), "partial": len(done) - len(completed),
            "skipped": len(skipped),
            "planned": sum(1 for r in rows if r["status"] == ex.PLANNED),
            "ladders_in_progress": in_progress,
            "steps_attempted": len({r["ladder_item_id"] for r in done if r["ladder_item_id"]}),
            "steps_completed": len(done_steps),
            "highest_step": highest,
        },
        "weeks": _weeks(done, start, end),
        "prediction": {
            "entries": len(likely),
            "predicted_likelihood": _mean(r["predicted_likelihood"] for r in likely) if len(likely) >= MIN_DONE else None,
            "outcome_rate": _mean(OUTCOME_SCORE[r["outcome_occurred"]] for r in likely) if len(likely) >= MIN_DONE else None,
        },
        "anxiety": {
            "predicted": enough(done, "predicted_anxiety"),
            "peak": enough(done, "peak"),
            "end": enough(done, "end"),
        },
        "repeats": repeats,
        "safety_planned": _top(Counter(s["name"] for r in done for s in r["safety_behaviors_planned"])),
        "safety_used": _top(Counter(s["name"] for r in done for s in r["safety_behaviors_used"])),
        "pattern": ex.no_change(rows),
    }
    if len(done) < MIN_DONE:
        needed = MIN_DONE - len(done)
        report["insights"] = None
        report["locked"] = {"needed": needed, "text": TEXT["locked"].format(n=needed)}
    else:
        report["insights"] = _insights(report)
        report["locked"] = None
    return report


def _insights(report: dict) -> list[str]:
    out = []
    p = report["prediction"]
    if p["outcome_rate"] is not None:
        gap = p["outcome_rate"] - p["predicted_likelihood"]
        key = "outcome_same" if abs(gap) < OUTCOME_SAME else "outcome_less" if gap < 0 else "outcome_more"
        out.append(_say(key, predicted=round(p["predicted_likelihood"]), actual=round(p["outcome_rate"])))
    a = report["anxiety"]
    if a["predicted"] is not None and a["peak"] is not None and a["end"] is not None:
        out.append(_say("peak_lower" if a["peak"] < a["predicted"] else "peak_higher",
                        peak=round(a["peak"]), predicted=round(a["predicted"]), end=round(a["end"])))
    if report["repeats"]:
        first = _mean(r["first_peak"] for r in report["repeats"])
        last = _mean(r["last_peak"] for r in report["repeats"])
        if first - last >= REPEAT_DROP:
            out.append(_say("repeats", first=round(first), last=round(last)))
    if report["safety_planned"] and report["safety_planned"][0]["count"] >= 2:
        out.append(_say("safety", behaviour=report["safety_planned"][0]["name"]))
    s = report["summary"]
    if s["partial"] or s["skipped"]:
        out.append(_say("partial", partial=s["partial"], skipped=s["skipped"]))
    return out


# ---------- the combined clinical report, the shared events, her export ----------

EXCERPT_FIELDS = ("local_date", "feared_outcome", "predicted_likelihood", "outcome_occurred", "what_happened", "learning")


def exposure_section(db: Session, account_id, period, consent) -> dict:
    """
    Section C of the combined clinical report: the report's metrics, the
    no-change pattern from ratings alone, her words only with
    consent.include_excerpts. Never raises: empty, or anything that goes wrong,
    comes back as empty.
    """
    empty = {"empty": True, "note": TEXT["empty_section"]}
    try:
        start, end = savoring_report._dates(period)
        terms = savoring_report._terms(db, account_id)
        rows = in_period(ex.described(db, account_id, terms, 0), start, end)
        if not any(r["status"] in ex.FINISHED for r in rows):
            return empty
        report = build(rows, ex.ladders(db, account_id, rows, include_archived=True), start, end)
        section = {
            "section": "C",
            "title": "Exposure Journal",
            "period": {"from": start.isoformat(), "to": end.isoformat()},
            **{k: report["summary"][k] for k in ("done", "completed", "partial", "skipped", "ladders_in_progress",
                                                  "steps_attempted", "steps_completed")},
            "highest_step_completed": (report["summary"]["highest_step"] or {}).get("expected_suds"),
            "exposures_per_week": report["weeks"],
            "predicted_vs_actual": report["prediction"],
            "anxiety_predicted_peak_end": report["anxiety"],
            "repeated_steps": [{k: v for k, v in r.items() if k not in ("step", "fear")} for r in report["repeats"]],
            "safety_behaviours_planned": report["safety_planned"],
            "safety_behaviours_used": report["safety_used"],
            "no_change_pattern": (
                {"attempts": report["pattern"]["attempts"],
                 "text": TEXT["pattern"].format(step=report["pattern"]["step"], n=report["pattern"]["attempts"])}
                if report["pattern"] else None
            ),
        }
        include = consent.get("include_excerpts") if isinstance(consent, dict) else getattr(consent, "include_excerpts", False)
        if include:
            finished = [r for r in rows if r["status"] in ex.COUNTED][:5]
            section["excerpts"] = [{**{k: r[k] for k in EXCERPT_FIELDS},
                                    "step": (r["step"] or {}).get("description") or r["planned_activity"]}
                                   for r in finished]
        return section
    except Exception:  # noqa: BLE001 — the composer must always get a section
        logger.exception("exposure_section could not be built")  # no entry text in the log
        return {**empty, "note": "Exposure Journal section could not be built"}


def journal_events(db: Session, account_id, start: date, end: date) -> list[dict]:
    """This journal's journal_events rows: mood_signal is how far the peak fell short of the prediction, on 0-10."""
    rows = in_period(ex.described(db, account_id, {}, 0, statuses=ex.COUNTED), start, end)
    return [{"user_id": str(account_id), "module": "exposure", "entry_id": r["id"], "local_date": r["local_date"],
             "created_at": r["created_at"],
             "mood_signal": (round((r["predicted_anxiety"] - r["peak"]) / 10, 1)
                             if r["predicted_anxiety"] is not None and r["peak"] is not None else None)}
            for r in rows]


NOTE_FIELDS = ("feared_outcome", "learning_goal", "what_happened", "learning", "planned_activity")


def export(rows: list[dict], ladders: list[dict], include_notes: bool) -> dict:
    """Her ladders with their steps, and every exposure. Her own notes only when she chose them."""
    def record(r):
        out = {k: v for k, v in r.items() if k not in NOTE_FIELDS and k not in ("step", "client_id")}
        out["step"] = (r["step"] or {}).get("description")
        if include_notes:
            out.update({k: r[k] for k in NOTE_FIELDS})
        return out

    return {
        "disclaimer": "Self-reported data, not a diagnosis.",
        "ladders": [{"fear_name": l["fear_name"], "tags": [t["name"] for t in l["tags"]], "archived": l["archived"],
                     "steps": [{k: s[k] for k in ("description", "expected_suds", "sort_order", "archived")}
                               for s in l["steps"]]}
                    for l in ladders],
        "exposures": [record(r) for r in sorted(rows, key=_when)],
    }
