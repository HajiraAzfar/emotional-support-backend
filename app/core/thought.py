"""
The Thought Journal: a CBT thought record. She catches an upsetting automatic
thought, looks at the evidence, writes a balanced alternative, and sees how
her belief and feelings change. Curious and gentle: nothing here ever calls a
thought wrong.

Stored on the shared engine: an entries row (journal_type = 'thought',
client_id, local_date, source_entry_id, is_favorite, edited_at) and
captured_values under the journal's ids from before (situation,
automatic_thought, feelings, emotions_before / emotions_after per feeling,
thinking_traps, supporting_evidence, contradicting_evidence, revised_thought)
and new ones (thought_mode, situation_tags, second_thought, belief_before,
belief_after, balanced_belief, next_step). A record from before has no beliefs
or tags: every average skips what is missing, and FIELDS names everything the
way the app and the clinical report read it.

Drafts are in_progress entries keyed by the app's client_id, saved at every
step (the app keeps a copy, so a step taken offline is never lost) and gone
after DRAFT_DAYS. Only completed records count anywhere. A finished record
can be changed freely for EDIT_HOURS; later changes are marked edited_at.

Safety: free text that changed in a save is screened at once, as in every
journal, so each step and the final save are both screened. From GENTLE_AFTER
completed records in a day she gets a kind word, never a block. A theme that
keeps coming back with the belief staying high is a pattern: the app suggests
someone to talk to, and the clinical section notes it from counts alone.

AI assist only when she asks: one Socratic question at the evidence, and up to
two thinking patterns for her to confirm. Never advice, a diagnosis, a label
or the balanced thought; rate-limited; the app has fixed questions otherwise.
"""
import uuid
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from app.core import capture, crisis, insights, journal_records as records, llm_client, slots, streaks
from app.models.entry import Entry

JOURNAL = "thought"
QUICK, FULL = "quick", "full"
DRAFT_DAYS = 7
EDIT_HOURS = 24
GENTLE_AFTER = 3
# A check-in that opens the journal: stress at or above this, or mood at or below.
HIGH_ANXIETY, LOW_MOOD = 7, 2
# A recurring theme: this many records in PATTERN_DAYS sharing a tag and a
# pattern, each still believed at least PATTERN_BELIEF with a drop under PATTERN_DROP.
PATTERN_RECORDS, PATTERN_DAYS, PATTERN_BELIEF, PATTERN_DROP = 3, 30, 70, 10
# AI assist: at most this many calls per account in the window.
ASSIST_CALLS, ASSIST_WINDOW = 8, timedelta(minutes=15)

TAGS, TRAPS, FEELINGS = "thought_situation_tags", "thinking_traps", "feelings"

# Stored ids (left) and the names the app and the clinical report use (right).
FIELDS = {
    "situation": "situation",
    "situation_tags": "situation_tags",
    "automatic_thought": "automatic_thought",
    "second_thought": "second_thought",
    "belief_before": "belief_before",
    "belief_after": "belief_after",
    "thinking_traps": "thinking_traps",
    "supporting_evidence": "evidence_for",
    "contradicting_evidence": "evidence_against",
    "revised_thought": "balanced_thought",
    "balanced_belief": "balanced_belief",
    "next_step": "next_step",
}
STORED = {name: key for key, name in FIELDS.items()}
V_MODE, V_FEELINGS, V_BEFORE, V_AFTER = "thought_mode", "feelings", "emotions_before", "emotions_after"
CONTENT_VALUES = tuple(FIELDS) + (V_FEELINGS, V_BEFORE, V_AFTER)
FREE_TEXT = ("situation", "automatic_thought", "second_thought", "supporting_evidence",
             "contradicting_evidence", "revised_thought", "next_step")

_PROMPTS = {
    part.split("\n", 1)[0].strip(): part.split("\n", 1)[1].strip()
    for part in (Path(__file__).resolve().parent.parent / "thought_assist.md").read_text(encoding="utf-8").split("\n# ")[1:]
}
# ponytail: per process, like the login rate limit; a shared store if the server runs several.
_assist_calls: dict[str, list[datetime]] = {}


Refused = records.Refused


# ---------- reading and checking ----------

def _text(value) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def asks_supporting_evidence(focus_codes: list[str]) -> bool:
    """The trauma variant (FR-JRN-005) leaves "what makes it feel true" out: it can entrench the belief."""
    return any(v["id"] == "supporting_evidence" for v in capture.values_for(JOURNAL, focus_codes))


def situation_required(focus_codes: list[str]) -> bool:
    return next(v for v in capture.values_for(JOURNAL, focus_codes) if v["id"] == "situation")["required"]


def validate(content, terms: dict[str, str], focus_codes: list[str], complete: bool) -> dict:
    """Stored id → value, for what she gave. ValueError for what the app should never send."""
    def known(ids, library, what):
        out = []
        for i in ids:
            if i not in capture.LIBRARY_NAMES[library] and not (library == FEELINGS and i in terms):
                raise ValueError(f"Unknown {what}: {i}")
            if i not in out:
                out.append(i)
        return out

    feelings = known([e.feeling for e in content.emotions], FEELINGS, "feeling")
    by_feeling = {e.feeling: e for e in content.emotions}
    values = {
        "situation": _text(content.situation),
        "situation_tags": known(content.situation_tags, TAGS, "situation tag"),
        "automatic_thought": _text(content.automatic_thought),
        "second_thought": _text(content.second_thought),
        "belief_before": content.belief_before,
        "belief_after": content.belief_after,
        "thinking_traps": known(content.thinking_traps, TRAPS, "thinking pattern"),
        "supporting_evidence": _text(content.evidence_for) if asks_supporting_evidence(focus_codes) else None,
        "contradicting_evidence": _text(content.evidence_against),
        "revised_thought": _text(content.balanced_thought),
        "balanced_belief": content.balanced_belief,
        "next_step": _text(content.next_step),
        V_FEELINGS: feelings,
        V_BEFORE: {f: by_feeling[f].before for f in feelings if by_feeling[f].before is not None},
        V_AFTER: {f: by_feeling[f].after for f in feelings if by_feeling[f].after is not None},
    }
    if complete:
        if values["automatic_thought"] is None:
            raise ValueError("Write the thought before finishing")
        if values["situation"] is None and situation_required(focus_codes):
            raise ValueError("Say a little about what was happening before finishing")
    return {k: v for k, v in values.items() if v not in (None, "", [], {})}


def recorded_values(db: Session, entries: list[Entry]) -> dict:
    return insights._values(db, entries)


def _store(db: Session, entry: Entry, values: dict):
    records.store(db, entry, values, CONTENT_VALUES + (V_MODE,))


def _screen(db: Session, account, entry: Entry, old: dict, new: dict):
    return records.screen_changes(db, account, entry, old, new, FREE_TEXT)


# ---------- drafts, finishing, editing ----------

def _entry_for(db: Session, account, client_id: uuid.UUID) -> Entry | None:
    return records.entry_for(db, account, client_id, JOURNAL)


def checkin_feelings(db: Session, account_id, source_id) -> list[str]:
    """A check-in's feelings, to start a record opened from it."""
    return records.source_feelings(db, account_id, source_id, ("check_in",),
                                   "A thought record can only be opened from a check-in.")


def save(db: Session, account, client_id: uuid.UUID, payload, terms: dict[str, str], focus_codes: list[str],
         now_utc: datetime | None = None):
    """
    A draft step, the finished record, or a later change, all by the app's
    client id (so a retry or an offline sync is the same record). Returns
    (entry, crisis result).
    """
    now = now_utc or datetime.utcnow()
    entry = _entry_for(db, account, client_id)
    if entry is None:
        if payload.source_entry_id is not None:
            checkin_feelings(db, account.id, payload.source_entry_id)  # it must be her check-in
        entry = Entry(
            account_id=account.id, journal_type=JOURNAL, status="in_progress", started_at=now, client_id=client_id,
            local_date=slots.local_now(payload.tz_offset, now_utc).date(), tz_offset=payload.tz_offset,
            timezone=payload.timezone, source_entry_id=payload.source_entry_id,
        )
        db.add(entry)
        db.flush()
    return _apply(db, account, entry, payload, payload.complete or entry.status == "completed", payload.mode,
                  terms, focus_codes, now, payload.tz_offset)


def edit(db: Session, account, entry: Entry, content, terms: dict[str, str], focus_codes: list[str],
         now_utc: datetime | None = None):
    """A change to a finished record (one from before the module included)."""
    recorded = recorded_values(db, [entry]).get(entry.id, {})
    return _apply(db, account, entry, content, True, recorded.get(V_MODE, FULL), terms, focus_codes,
                  now_utc or datetime.utcnow(), entry.tz_offset or 0)


def _apply(db: Session, account, entry: Entry, content, complete: bool, mode: str, terms, focus_codes,
           now: datetime, tz_offset: int):
    old = recorded_values(db, [entry]).get(entry.id, {})
    values = validate(content, terms, focus_codes, complete)
    _store(db, entry, {**values, V_MODE: mode})
    if complete and entry.status != "completed":
        entry.status = "completed"
        entry.completed_at = now
        # The day she finished it is the day it counts for.
        entry.local_date = slots.local_now(tz_offset, now).date()
    elif entry.status == "completed" and entry.completed_at and now - entry.completed_at > timedelta(hours=EDIT_HOURS):
        entry.edited_at = now
    # Saved before anything reads it (FR-ENT-003).
    db.commit()
    result = _screen(db, account, entry, old, values)
    db.commit()
    db.refresh(entry)
    return entry, result


client_id_of = records.client_id_of


def expire_drafts(db: Session, account_id, now_utc: datetime | None = None) -> int:
    return records.expire_drafts(db, account_id, JOURNAL, DRAFT_DAYS, now_utc)


# ---------- what the app reads ----------

def entries_of(db: Session, account_id, status: str | None = "completed") -> list[Entry]:
    query = db.query(Entry).filter(Entry.account_id == account_id, Entry.journal_type == JOURNAL)
    if status:
        query = query.filter(Entry.status == status)
    return query.order_by(Entry.started_at).all()


day_of = records.day_of


def _drop(before, after) -> int | None:
    return before - after if isinstance(before, int) and isinstance(after, int) else None


def emotion_drop(rec: dict) -> float | None:
    """How much the feelings eased (0-100 points), averaged over those rated both times."""
    before, after = rec.get(V_BEFORE) or {}, rec.get(V_AFTER) or {}
    pairs = [before[f] - after[f] for f in before if f in after]
    return round(sum(pairs) / len(pairs), 1) if pairs else None


def describe(entry: Entry, rec: dict, terms: dict[str, str], tz_offset: int, now_utc: datetime | None = None) -> dict:
    """One record as the app shows it, by the names in FIELDS; a record from before reads the same way."""
    def named(library, ids):
        return [{"id": i, "name": insights._label(library, i, terms)} for i in ids or []]

    before, after = rec.get(V_BEFORE) or {}, rec.get(V_AFTER) or {}
    out = {FIELDS[k]: rec.get(k) for k in FIELDS}
    offset = entry.tz_offset if entry.tz_offset is not None else tz_offset
    moment = (entry.completed_at or entry.started_at) + timedelta(minutes=offset)
    now = now_utc or datetime.utcnow()
    out.update({
        "id": str(entry.id),
        "client_id": str(entry.client_id) if entry.client_id else None,
        "status": entry.status,
        "mode": rec.get(V_MODE, FULL),
        "name": rec.get("name"),
        "local_date": day_of(entry, tz_offset).isoformat(),
        "created_at": moment.isoformat(timespec="minutes"),
        "edited_at": entry.edited_at.isoformat(timespec="minutes") if entry.edited_at else None,
        "editable_freely": bool(entry.completed_at) and now - entry.completed_at <= timedelta(hours=EDIT_HOURS),
        "is_favorite": bool(entry.is_favorite),
        "source_entry_id": str(entry.source_entry_id) if entry.source_entry_id else None,
        "situation_tags": named(TAGS, rec.get("situation_tags")),
        "thinking_traps": named(TRAPS, rec.get("thinking_traps")),
        "emotions": [{"id": f, "name": insights._label(FEELINGS, f, terms), "before": before.get(f),
                      "after": after.get(f)} for f in rec.get(V_FEELINGS) or []],
        "belief_drop": _drop(rec.get("belief_before"), rec.get("belief_after")),
        "emotion_drop": emotion_drop(rec),
    })
    return out


def described(db: Session, account_id, terms, tz_offset: int, status: str | None = "completed") -> list[dict]:
    """Records newest first (completed only unless asked)."""
    entries = entries_of(db, account_id, status)
    recorded = recorded_values(db, entries)
    return [describe(e, recorded.get(e.id, {}), terms, tz_offset) for e in reversed(entries)]


def completed_on(rows: list[dict], day: date) -> int:
    return sum(1 for r in rows if r["status"] == "completed" and r["local_date"] == day.isoformat())


def recurring(rows: list[dict], today: date) -> dict | None:
    """
    A theme that keeps coming back without moving: PATTERN_RECORDS finished
    records in PATTERN_DAYS sharing a situation tag and a thinking pattern, each
    still believed at least PATTERN_BELIEF with a drop under PATTERN_DROP.
    Counted from her tags and patterns, never read from her words.
    """
    since = today - timedelta(days=PATTERN_DAYS)
    stuck = [r for r in rows if r["status"] == "completed" and date.fromisoformat(r["local_date"]) >= since
             and isinstance(r["belief_after"], int) and r["belief_after"] >= PATTERN_BELIEF
             and r["belief_drop"] is not None and r["belief_drop"] < PATTERN_DROP]
    pairs = Counter((t["name"], p["name"]) for r in stuck for t in r["situation_tags"] for p in r["thinking_traps"])
    if not pairs:
        return None
    (tag, trap), n = max(pairs.items(), key=lambda kv: kv[1])
    return {"tag": tag, "trap": trap, "records": n} if n >= PATTERN_RECORDS else None


def heatmap(rows: list[dict], start: date, end: date, today: date) -> dict:
    """A day counts with one finished record or more."""
    points = Counter(date.fromisoformat(r["local_date"]) for r in rows if r["status"] == "completed")
    return streaks.heatmap(dict(points), start, end, today)


# ---------- AI assist ----------

def _allowed(account_id) -> bool:
    now = datetime.utcnow()
    calls = [t for t in _assist_calls.get(str(account_id), []) if now - t < ASSIST_WINDOW]
    if len(calls) >= ASSIST_CALLS:
        _assist_calls[str(account_id)] = calls
        return False
    _assist_calls[str(account_id)] = calls + [now]
    return True


def _her_words(rec: dict, keys) -> str:
    return "\n".join(f"{FIELDS.get(k, k)}: <user_text>{rec[k]}</user_text>" for k in keys if rec.get(k))


def question(db: Session, entry: Entry, side: str) -> str | None:
    """One Socratic question about the evidence {side} the thought, or None (the app asks a fixed one)."""
    if crisis.suppressed(entry) or not _allowed(entry.account_id):
        return None
    rec = recorded_values(db, [entry]).get(entry.id, {})
    if not rec.get("automatic_thought"):
        return None
    prompt = _PROMPTS["Question"].replace("{side}", "for" if side == "for" else "against")
    words = _her_words(rec, ("situation", "automatic_thought", "supporting_evidence", "contradicting_evidence"))
    return llm_client.generate_socratic_question(f"{prompt}\n\n# Recorded so far\n{words}")


def suggest_traps(db: Session, entry: Entry) -> list[str] | None:
    """Up to two thinking patterns for her to confirm or reject, or None."""
    if crisis.suppressed(entry) or not _allowed(entry.account_id):
        return None
    rec = recorded_values(db, [entry]).get(entry.id, {})
    if not rec.get("automatic_thought"):
        return None
    listed = "\n".join(f"- {i}: {name}" for i, name in capture.LIBRARY_NAMES[TRAPS].items())
    words = _her_words(rec, ("situation", "automatic_thought"))
    return llm_client.suggest_traps(f"{_PROMPTS['Traps']}\n\n# Pattern ids\n{listed}\n\n# Her thought\n{words}",
                                    set(capture.LIBRARY_NAMES[TRAPS]))
