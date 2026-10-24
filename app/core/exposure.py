"""
The Exposure Journal: graded exposure with an inhibitory-learning approach.
She builds a fear ladder, predicts what will happen, does a step, and looks at
the gap between the prediction and what happened. Stopping, pausing or doing
part of a step is never a failure.

Ladders are their own tables (models/exposure_ladder.py). Exposures stay
entries (journal_type = 'exposure') with captured_values under the journal's
ids from before (feared_outcome, distress_before / _during / _after on 0-10,
planned_activity, post_account, learning) and new ones (exposure_mode,
exposure_status, ladder_item_id, predicted_likelihood / _severity /
_anxiety, safety_behaviors_planned / _used, learning_goal, scheduled_for,
anxiety_peak / _end / _readings on 0-100, outcome_occurred, next_step,
feelings). FIELDS names them for the app and the reports; a record from
before reads its 0-10 distress ten times over, during as the peak and after
as the end.

exposure_status is the journal's own state; entries.status follows it, set in
one place only (ENTRY_STATUS):
    draft, planned        -> in_progress
    completed, partial    -> completed
    skipped               -> closed   (out of streaks, the heatmap and averages)
"""
import json
import uuid
from collections import Counter
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from app.core import capture, insights, journal_records as records, slots, streaks
from app.models.captured_value import CapturedValue
from app.models.entry import Entry
from app.models.exposure_ladder import ExposureLadder, ExposureLadderItem

JOURNAL = "exposure"
DRAFT_DAYS = 7
EDIT_HOURS = 24
# A kind word from the second exposure finished in a day; never a block.
GENTLE_AFTER = 2
# Where a ladder suggests starting: a step expected at 30-50.
START_LOW, START_HIGH = 30, 50
MAX_STEPS = 10
# Onboarding distress at or above this (0-10) suggests doing exposures with a professional.
HIGH_BASELINE = 8
# No change: this many finished attempts at one step over at least PATTERN_SPAN
# days, with neither the predicted likelihood nor the peak coming down by PATTERN_CHANGE.
PATTERN_ATTEMPTS, PATTERN_SPAN, PATTERN_CHANGE = 4, 14, 5

DRAFT, PLANNED, COMPLETED, PARTIAL, SKIPPED = "draft", "planned", "completed", "partial", "skipped"
ENTRY_STATUS = {DRAFT: "in_progress", PLANNED: "in_progress", COMPLETED: "completed", PARTIAL: "completed",
                SKIPPED: "closed"}
FINISHED = (COMPLETED, PARTIAL, SKIPPED)
COUNTED = (COMPLETED, PARTIAL)
# Forward only: a plan can be done, done in part or let go; a finished record
# can be corrected between those, never sent back to a plan.
NEXT = {DRAFT: {DRAFT, PLANNED, *FINISHED}, PLANNED: {PLANNED, *FINISHED}, COMPLETED: set(FINISHED),
        PARTIAL: set(FINISHED), SKIPPED: set(FINISHED)}

TAGS, SAFETY, FEELINGS = "exposure_tags", "safety_behaviours", "feelings"
V_MODE, V_STATUS = "exposure_mode", "exposure_status"
# Stored ids (left) and the names the app and the reports use (right).
FIELDS = {
    "ladder_item_id": "ladder_item_id",
    "feared_outcome": "feared_outcome",
    "predicted_likelihood": "predicted_likelihood",
    "predicted_severity": "predicted_severity",
    "predicted_anxiety": "predicted_anxiety",
    "safety_behaviors_planned": "safety_behaviors_planned",
    "learning_goal": "learning_goal",
    "scheduled_for": "scheduled_for",
    "feelings": "feelings",
    "anxiety_peak": "anxiety_peak",
    "anxiety_end": "anxiety_end",
    "anxiety_readings": "anxiety_readings",
    "post_account": "what_happened",
    "outcome_occurred": "outcome_occurred",
    "safety_behaviors_used": "safety_behaviors_used",
    "learning": "learning",
    "next_step": "next_step",
}
STORED = {name: key for key, name in FIELDS.items()}
CONTENT_VALUES = tuple(FIELDS) + (V_MODE, V_STATUS)
FREE_TEXT = ("feared_outcome", "learning_goal", "post_account", "learning")
# From before: 0-10 distress, read ten times over.
LEGACY = {"distress_before": "before", "distress_during": "peak", "distress_after": "end"}

Refused = records.Refused


def _when(r: dict) -> tuple:
    """Oldest first: by her day, then by the time it was saved."""
    return r["local_date"], r["created_at"]


# ---------- ladders ----------

def _tags(ids: list[str]) -> list[str]:
    out = []
    for i in ids:
        if i not in capture.LIBRARY_NAMES[TAGS]:
            raise ValueError(f"Unknown tag: {i}")
        if i not in out:
            out.append(i)
    return out


def _ladder(db: Session, account_id, ladder_id) -> ExposureLadder:
    ladder = db.query(ExposureLadder).filter(ExposureLadder.id == ladder_id,
                                             ExposureLadder.account_id == account_id).first()
    if ladder is None:
        raise Refused("not_found", "Ladder not found")
    return ladder


def _items(db: Session, ladder_id, include_archived: bool = False) -> list[ExposureLadderItem]:
    query = db.query(ExposureLadderItem).filter(ExposureLadderItem.ladder_id == ladder_id)
    if not include_archived:
        query = query.filter(ExposureLadderItem.archived.is_(False))
    return query.order_by(ExposureLadderItem.sort_order, ExposureLadderItem.created_at).all()


def _linked(db: Session, account_id, item_ids: list) -> set[str]:
    """Steps some exposure of hers points at (those are archived, never deleted)."""
    wanted = {json.dumps(str(i)) for i in item_ids}
    rows = db.query(CapturedValue.value).join(Entry, Entry.id == CapturedValue.entry_id).filter(
        Entry.account_id == account_id, CapturedValue.key == "ladder_item_id", CapturedValue.value.in_(wanted)
    ).all()
    return {json.loads(v) for (v,) in rows}


def item_of(db: Session, account_id, item_id) -> ExposureLadderItem | None:
    """A step, only if it is on one of her ladders."""
    return db.query(ExposureLadderItem).join(ExposureLadder, ExposureLadder.id == ExposureLadderItem.ladder_id).filter(
        ExposureLadderItem.id == item_id, ExposureLadder.account_id == account_id).first()


def _screen_words(db: Session, account, words: list[str]):
    return records.screen_text(db, account, "ladder", "\n".join(w for w in words if w))


def create_ladder(db: Session, account, payload):
    """A new ladder; its steps sorted from easy to hard by how anxious she expects each to make her."""
    tags = _tags(payload.tags)
    ladder = ExposureLadder(account_id=account.id, fear_name=payload.fear_name.strip(), tags=json.dumps(tags))
    db.add(ladder)
    db.flush()
    for n, step in enumerate(sorted(payload.steps, key=lambda s: s.expected_suds)):
        db.add(ExposureLadderItem(ladder_id=ladder.id, description=step.description.strip(),
                                  expected_suds=step.expected_suds, sort_order=n))
    db.commit()
    result = _screen_words(db, account, [payload.fear_name] + [s.description for s in payload.steps])
    db.commit()
    return ladder, result


def update_ladder(db: Session, account, ladder_id, payload):
    ladder = _ladder(db, account.id, ladder_id)
    if payload.fear_name is not None:
        ladder.fear_name = payload.fear_name.strip()
    if payload.tags is not None:
        ladder.tags = json.dumps(_tags(payload.tags))
    if payload.archived is not None:
        ladder.archived = payload.archived
    db.commit()
    result = _screen_words(db, account, [payload.fear_name or ""])
    db.commit()
    return ladder, result


def add_step(db: Session, account, ladder_id, step):
    """A step goes where its expected anxiety puts it; she can move it after."""
    ladder = _ladder(db, account.id, ladder_id)
    items = _items(db, ladder.id)
    if len(items) >= MAX_STEPS:
        raise Refused("too_many", f"A ladder holds up to {MAX_STEPS} steps.")
    place = next((n for n, item in enumerate(items) if item.expected_suds > step.expected_suds), len(items))
    new = ExposureLadderItem(ladder_id=ladder.id, description=step.description.strip(),
                             expected_suds=step.expected_suds, sort_order=place)
    db.add(new)
    for item in items[place:]:
        item.sort_order += 1
    db.commit()
    result = _screen_words(db, account, [step.description])
    db.commit()
    return new, result


def update_step(db: Session, account, ladder_id, item_id, payload):
    ladder = _ladder(db, account.id, ladder_id)
    item = next((i for i in _items(db, ladder.id, True) if i.id == item_id), None)
    if item is None:
        raise Refused("not_found", "Step not found")
    if payload.description is not None:
        item.description = payload.description.strip()
    if payload.expected_suds is not None:
        item.expected_suds = payload.expected_suds
    db.commit()
    result = _screen_words(db, account, [payload.description or ""])
    db.commit()
    return item, result


def reorder(db: Session, account, ladder_id, item_ids: list):
    """Her own order: every step of the ladder (archived ones aside), each once."""
    ladder = _ladder(db, account.id, ladder_id)
    items = {i.id: i for i in _items(db, ladder.id)}
    if len(item_ids) != len(items) or set(item_ids) != set(items):
        raise Refused("bad_order", "The order must list every step of the ladder once.")
    for n, item_id in enumerate(item_ids):
        items[item_id].sort_order = n
    db.commit()


def remove_step(db: Session, account, ladder_id, item_id) -> str:
    """'archived' when an exposure points at the step (its name stays in records and reports), else 'deleted'."""
    ladder = _ladder(db, account.id, ladder_id)
    item = next((i for i in _items(db, ladder.id, True) if i.id == item_id), None)
    if item is None:
        raise Refused("not_found", "Step not found")
    if str(item.id) in _linked(db, account.id, [item.id]):
        item.archived = True
        outcome = "archived"
    else:
        db.delete(item)
        outcome = "deleted"
    db.commit()
    return outcome


def remove_ladder(db: Session, account, ladder_id) -> str:
    """The same rule for a whole ladder: archived if any of its steps was used."""
    ladder = _ladder(db, account.id, ladder_id)
    items = _items(db, ladder.id, True)
    if _linked(db, account.id, [i.id for i in items]):
        ladder.archived = True
        for item in items:
            item.archived = True
        outcome = "archived"
    else:
        for item in items:
            db.delete(item)
        db.delete(ladder)
        outcome = "deleted"
    db.commit()
    return outcome


def ladders(db: Session, account_id, rows: list[dict] | None = None, include_archived: bool = False) -> list[dict]:
    """Her ladders with their steps in order; with the records, how each step has gone so far."""
    query = db.query(ExposureLadder).filter(ExposureLadder.account_id == account_id)
    if not include_archived:
        query = query.filter(ExposureLadder.archived.is_(False))
    by_step: dict[str, list[dict]] = {}
    for r in rows or []:
        if r["ladder_item_id"] and r["status"] in COUNTED:
            by_step.setdefault(r["ladder_item_id"], []).append(r)
    out = []
    for ladder in query.order_by(ExposureLadder.created_at).all():
        steps = []
        for item in _items(db, ladder.id, include_archived):
            tried = sorted(by_step.get(str(item.id), []), key=_when)
            steps.append({
                "id": str(item.id), "description": item.description, "expected_suds": item.expected_suds,
                "sort_order": item.sort_order, "archived": item.archived,
                "attempts": len(tried), "completed": sum(1 for r in tried if r["status"] == COMPLETED),
                "last_peak": tried[-1]["peak"] if tried else None,
                "suggested_start": START_LOW <= item.expected_suds <= START_HIGH,
            })
        out.append({"id": str(ladder.id), "fear_name": ladder.fear_name, "archived": ladder.archived,
                    "tags": [{"id": t, "name": capture.LIBRARY_NAMES[TAGS].get(t, t)} for t in json.loads(ladder.tags or "[]")],
                    "steps": steps})
    return out


def step_lookup(db: Session, account_id) -> dict[str, dict]:
    """Every step of hers by id, archived ones too, with its ladder's fear."""
    rows = db.query(ExposureLadderItem, ExposureLadder).join(
        ExposureLadder, ExposureLadder.id == ExposureLadderItem.ladder_id).filter(
        ExposureLadder.account_id == account_id).all()
    return {str(item.id): {"id": str(item.id), "description": item.description, "expected_suds": item.expected_suds,
                           "archived": item.archived, "ladder_id": str(ladder.id), "fear_name": ladder.fear_name}
            for item, ladder in rows}


# ---------- exposure records ----------

def _text(value) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _known(ids: list[str], library: str, what: str, terms: dict | None = None) -> list[str]:
    out = []
    for i in ids:
        if i not in capture.LIBRARY_NAMES[library] and not (terms and i in terms):
            raise ValueError(f"Unknown {what}: {i}")
        if i not in out:
            out.append(i)
    return out


def validate(db: Session, account_id, content, terms: dict, status: str, legacy: bool) -> dict:
    """Stored id → value, for what she gave. ValueError for what the app should never send."""
    if content.ladder_item_id is not None and item_of(db, account_id, content.ladder_item_id) is None:
        raise ValueError("That step is not on one of your ladders")
    values = {
        "ladder_item_id": str(content.ladder_item_id) if content.ladder_item_id else None,
        "feared_outcome": _text(content.feared_outcome),
        "predicted_likelihood": content.predicted_likelihood,
        "predicted_severity": content.predicted_severity,
        "predicted_anxiety": content.predicted_anxiety,
        "safety_behaviors_planned": _known(content.safety_behaviors_planned, SAFETY, "safety behaviour"),
        "learning_goal": _text(content.learning_goal),
        "scheduled_for": content.scheduled_for,
        "feelings": _known(content.feelings, FEELINGS, "feeling", terms),
        "anxiety_peak": content.anxiety_peak,
        "anxiety_end": content.anxiety_end,
        "anxiety_readings": [{"minute": r.minute, "suds": r.suds} for r in content.anxiety_readings],
        "post_account": _text(content.what_happened),
        "outcome_occurred": content.outcome_occurred,
        "safety_behaviors_used": _known(content.safety_behaviors_used, SAFETY, "safety behaviour"),
        "learning": _text(content.learning),
        "next_step": content.next_step,
    }
    if status != DRAFT:
        # A plan, or a record, says what she fears and which step it is (a record from before had its own plan).
        if values["feared_outcome"] is None:
            raise ValueError("Say what you are afraid will happen")
        if values["ladder_item_id"] is None and not legacy:
            raise ValueError("Pick a step from a ladder")
    return {k: v for k, v in values.items() if v not in (None, "", [], {})}


def status_of(entry: Entry, rec: dict) -> str:
    """The journal's state; a record from before has none stored and is read from where it stopped."""
    if rec.get(V_STATUS):
        return rec[V_STATUS]
    if entry.status == "completed":
        return COMPLETED
    return PLANNED if rec.get("planned_activity") else DRAFT


def consistent(entry: Entry, rec: dict) -> bool:
    """entries.status always follows exposure_status."""
    return ENTRY_STATUS[status_of(entry, rec)] == entry.status


def source_feelings(db: Session, account_id, source_id) -> list[str]:
    return records.source_feelings(db, account_id, source_id, ("check_in", "thought"),
                                   "An exposure can only be planned from a check-in or a thought record.")


def save(db: Session, account, client_id: uuid.UUID, payload, terms: dict, now_utc: datetime | None = None):
    """
    A draft step, the finished plan, how it went, or a later correction, all by
    the app's client id. Returns (entry, crisis result).
    """
    now = now_utc or datetime.utcnow()
    entry = records.entry_for(db, account, client_id, JOURNAL)
    if entry is None:
        if payload.source_entry_id is not None:
            source_feelings(db, account.id, payload.source_entry_id)  # it must be hers
        entry = Entry(
            account_id=account.id, journal_type=JOURNAL, status="in_progress", started_at=now, client_id=client_id,
            local_date=slots.local_now(payload.tz_offset, now_utc).date(), tz_offset=payload.tz_offset,
            timezone=payload.timezone, source_entry_id=payload.source_entry_id,
            # Exposure has no scope notice to wait on in this flow: the app shows its own screening note.
            notice_acknowledged=True,
        )
        db.add(entry)
        db.flush()
    old = records_of(db, [entry]).get(entry.id, {})
    before, after = status_of(entry, old), payload.status
    if after not in NEXT[before]:
        raise Refused("bad_transition", "That record can't go back to a plan.")
    legacy = bool(old.get("planned_activity") or old.get("distress_before") is not None)
    values = validate(db, account.id, payload, terms, after, legacy)
    records.store(db, entry, {**values, V_MODE: payload.mode, V_STATUS: after}, CONTENT_VALUES)
    # The one place entries.status is set for this journal.
    entry.status = ENTRY_STATUS[after]
    if after in FINISHED and before not in FINISHED:
        entry.completed_at = now
        entry.local_date = slots.local_now(payload.tz_offset, now).date()
    elif before in FINISHED and entry.completed_at and now - entry.completed_at > timedelta(hours=EDIT_HOURS):
        entry.edited_at = now
    db.commit()
    result = records.screen_changes(db, account, entry, old, values, FREE_TEXT)
    db.commit()
    db.refresh(entry)
    return entry, result


def records_of(db: Session, entries: list[Entry]) -> dict:
    return insights._values(db, entries)


def expire_drafts(db: Session, account_id, now_utc: datetime | None = None) -> int:
    """Drafts and plans are kept a week; older ones go (as the spec asks, a scheduled one too)."""
    return records.expire_drafts(db, account_id, JOURNAL, DRAFT_DAYS, now_utc)


client_id_of = records.client_id_of
day_of = records.day_of


def _scaled(rec: dict, new: str, old: str) -> int | None:
    """A 0-100 value, or a value from before on 0-10, ten times over."""
    if isinstance(rec.get(new), int):
        return rec[new]
    return rec[old] * 10 if isinstance(rec.get(old), int) else None


def describe(entry: Entry, rec: dict, terms: dict, tz_offset: int, steps: dict[str, dict],
             now_utc: datetime | None = None) -> dict:
    """One exposure as the app shows it, by the names in FIELDS; one from before reads the same way."""
    def named(library, ids):
        return [{"id": i, "name": insights._label(library, i, terms)} for i in ids or []]

    out = {FIELDS[k]: rec.get(k) for k in FIELDS}
    offset = entry.tz_offset if entry.tz_offset is not None else tz_offset
    moment = (entry.completed_at or entry.started_at) + timedelta(minutes=offset)
    now = now_utc or datetime.utcnow()
    legacy = "exposure_status" not in rec
    out.update({
        "id": str(entry.id),
        "client_id": str(entry.client_id) if entry.client_id else None,
        "status": status_of(entry, rec),
        "mode": rec.get(V_MODE, "full"),
        "legacy": legacy,
        "planned_activity": rec.get("planned_activity"),
        "local_date": day_of(entry, tz_offset).isoformat(),
        "created_at": moment.isoformat(timespec="minutes"),
        "edited_at": entry.edited_at.isoformat(timespec="minutes") if entry.edited_at else None,
        "editable_freely": bool(entry.completed_at) and now - entry.completed_at <= timedelta(hours=EDIT_HOURS),
        "is_favorite": bool(entry.is_favorite),
        "source_entry_id": str(entry.source_entry_id) if entry.source_entry_id else None,
        "step": steps.get(rec.get("ladder_item_id")),
        "safety_behaviors_planned": named(SAFETY, rec.get("safety_behaviors_planned")),
        "safety_behaviors_used": named(SAFETY, rec.get("safety_behaviors_used")),
        "feelings": named(FEELINGS, rec.get("feelings")),
        "anxiety_readings": rec.get("anxiety_readings") or [],
        # 0-100 for every record: older ones were rated 0-10.
        "before": _scaled(rec, "predicted_anxiety", "distress_before") if legacy else None,
        "peak": _scaled(rec, "anxiety_peak", "distress_during"),
        "end": _scaled(rec, "anxiety_end", "distress_after"),
    })
    return out


def described(db: Session, account_id, terms: dict, tz_offset: int, statuses: tuple | None = None) -> list[dict]:
    """Records newest first, filtered by exposure_status when asked."""
    entries = db.query(Entry).filter(Entry.account_id == account_id, Entry.journal_type == JOURNAL).order_by(
        Entry.started_at).all()
    recorded = records_of(db, entries)
    steps = step_lookup(db, account_id)
    rows = [describe(e, recorded.get(e.id, {}), terms, tz_offset, steps) for e in reversed(entries)]
    return [r for r in rows if statuses is None or r["status"] in statuses]


def finished_on(rows: list[dict], day: date) -> int:
    """Exposures done (fully or partly) on a day: a skipped one is not counted."""
    return sum(1 for r in rows if r["status"] in COUNTED and r["local_date"] == day.isoformat())


def no_change(rows: list[dict]) -> dict | None:
    """
    The same step tried PATTERN_ATTEMPTS times or more over PATTERN_SPAN days,
    with neither what she predicted nor the peak coming down: a pattern for her
    to take to a professional. From her ratings, never her words.
    """
    by_step: dict[str, list[dict]] = {}
    for r in sorted((r for r in rows if r["status"] in COUNTED and r["ladder_item_id"]), key=_when):
        by_step.setdefault(r["ladder_item_id"], []).append(r)
    for attempts in by_step.values():
        if len(attempts) < PATTERN_ATTEMPTS:
            continue
        first, last = attempts[0], attempts[-1]
        span = (date.fromisoformat(last["local_date"]) - date.fromisoformat(first["local_date"])).days
        likelihood = (first["predicted_likelihood"], last["predicted_likelihood"])
        peak = (first["peak"], last["peak"])
        if span < PATTERN_SPAN or None in likelihood + peak:
            continue
        if likelihood[1] > likelihood[0] - PATTERN_CHANGE and peak[1] > peak[0] - PATTERN_CHANGE:
            step = first["step"] or {}
            return {"step": step.get("description"), "fear": step.get("fear_name"), "attempts": len(attempts)}
    return None


def heatmap(rows: list[dict], start: date, end: date, today: date) -> dict:
    """A day counts with an exposure done fully or partly; a skipped one counts nothing."""
    points = Counter(date.fromisoformat(r["local_date"]) for r in rows if r["status"] in COUNTED)
    return streaks.heatmap(dict(points), start, end, today)
