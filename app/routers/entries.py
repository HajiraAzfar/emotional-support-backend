"""
Entry engine.

Flow for a journal type with a capture schedule (check_in, savouring, thought, free_write):
  POST /entries                     → entry created, first capture prompt added
  POST /entries/{id}/captures       → one value recorded, next prompt added;
                                      after the last value the entry is complete
                                      and either the conversation offer is added,
                                      or (savouring) one closing message ends it
  POST /entries/{id}/conversation   → continue / stop the offer, or end the session
  POST /entries/{id}/messages       → one conversation turn
  GET  /entries                     → list (completed, or drafts to resume)
  DELETE /entries/{id}              → permanent removal

Every action inside an entry returns the whole entry (EntryStepOut) so the
client re-renders the thread from one source of truth.

Crisis: every free-text value is screened before it reaches any generated
output. A danger or emergency tier stops every AI call for that entry; the
entry itself can still be completed (FR-CRIS-004, 006, 007).
"""
import json
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.core import capture, context_builder, crisis, enforcement, language, llm_client
from app.core.config import settings
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.account import Account
from app.models.captured_value import CapturedValue
from app.models.crisis_event import CrisisEvent
from app.models.entry import Entry
from app.models.focus_area import FocusArea
from app.models.message import Message
from app.models.user_term import UserTerm
from app.schemas.entry import (
    CaptureAnswer,
    ConversationChoice,
    EntryCreate,
    EntryOut,
    EntryStepOut,
    EntrySummary,
)
from app.schemas.message import MessageCreate

router = APIRouter(prefix="/entries", tags=["entries"])

REPLY_UNAVAILABLE = (
    "I couldn't reply just now. What you wrote is saved — you can send another "
    "message in a moment, or end here."
)


# ---------- helpers ----------

def _get_entry(db: Session, entry_id: uuid.UUID, account: Account) -> Entry:
    entry = db.query(Entry).filter(
        Entry.id == entry_id,
        Entry.account_id == account.id,
    ).first()
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")
    return entry


def _messages(db: Session, entry: Entry) -> list[Message]:
    return db.query(Message).filter(
        Message.entry_id == entry.id
    ).order_by(Message.sequence).all()


def _add_message(db: Session, entry: Entry, role: str, kind: str, content: str, value_id: str | None = None) -> Message:
    last = db.query(Message).filter(Message.entry_id == entry.id).count()
    message = Message(
        entry_id=entry.id,
        role=role,
        kind=kind,
        value_id=value_id,
        content=content,
        sequence=last + 1,
    )
    db.add(message)
    db.flush()
    return message


def _recorded(db: Session, entry: Entry) -> dict:
    """value_id → typed value (None when skipped)."""
    rows = db.query(CapturedValue).filter(CapturedValue.entry_id == entry.id).all()
    return {r.key: (None if r.skipped or r.value is None else json.loads(r.value)) for r in rows}


def _focus_codes(db: Session, account: Account) -> list[str]:
    return [f.code for f in db.query(FocusArea).filter(FocusArea.account_id == account.id).all()]


def _user_terms(db: Session, account: Account) -> capture.Extra:
    """library → {item id → name} for the user's own terms."""
    extra: dict[str, dict[str, str]] = {}
    for term in db.query(UserTerm).filter(UserTerm.account_id == account.id).all():
        extra.setdefault(term.library, {})[term.item_id] = term.name
    return extra


def _free_text_answers(values: list[dict], recorded: dict) -> list[str]:
    """What the user wrote in her own words during capture — the best hint of her language."""
    return [
        recorded[spec["id"]]
        for spec in values
        if spec["control"] == "free_text" and recorded.get(spec["id"])
    ]


def _values(db: Session, entry: Entry, account: Account) -> list[dict]:
    """This user's schedule for this journal: the trauma variant differs (FR-JRN-005)."""
    journal_type = _journal_type(entry)
    if not capture.has_schedule(journal_type):
        return []
    return capture.values_for(journal_type, _focus_codes(db, account))


def _value_row(db: Session, entry: Entry, value_id: str) -> CapturedValue | None:
    return db.query(CapturedValue).filter(
        CapturedValue.entry_id == entry.id, CapturedValue.key == value_id
    ).first()


def _open_value(db: Session, entry: Entry, account: Account) -> dict | None:
    """
    FR-JRN-003: a repeatable value she is still writing — already recorded, but
    no prompt has followed it, so the same value stays open for another message.
    """
    if entry.status != "in_progress":
        return None
    spec = next((v for v in _values(db, entry, account) if v.get("repeatable")), None)
    if spec is None or _value_row(db, entry, spec["id"]) is None:
        return None
    last = db.query(Message).filter(
        Message.entry_id == entry.id,
        Message.kind.in_(("capture_answer", "capture_prompt")),
    ).order_by(Message.sequence.desc()).first()
    return spec if last is not None and last.kind == "capture_answer" and last.value_id == spec["id"] else None


def _paused(db: Session, entry: Entry) -> bool:
    """
    FR-JRN-006: she made a plan and the entry stopped there. It stays stopped
    until she comes back and says she has done it — asking "how did it go?"
    about something that has not happened yet is worse than asking nothing.
    """
    if entry.status != "in_progress":
        return False
    last = db.query(Message).filter(Message.entry_id == entry.id).order_by(Message.sequence.desc()).first()
    return last is not None and last.kind == "pause"


def _pause_due(values: list[dict], recorded: dict, spec: dict | None) -> bool:
    """True when the value she just answered is the one the schedule pauses after."""
    if spec is None:
        return False
    index = next((i for i, v in enumerate(values) if v["id"] == spec["id"]), 0)
    previous = values[index - 1] if index else None
    return bool(previous and capture.pauses_after(previous) and previous["id"] in recorded)


def _journal_type(entry: Entry) -> str:
    return context_builder.normalise_journal_type(entry.journal_type)


def _state(db: Session, entry: Entry, account: Account, crisis_event: crisis.CrisisResult | None = None, referral: bool = False) -> EntryStepOut:
    next_capture = None
    # FR-JRN-007: no value is requested until the scope notice is acknowledged.
    notice = capture.notice_for(_journal_type(entry), _focus_codes(db, account))
    pending_notice = notice if notice and not entry.notice_acknowledged else None
    # FR-JRN-006: nor while the entry is waiting for her to go and do it.
    pending_resume = _paused(db, entry)
    if entry.status == "in_progress" and not pending_notice and not pending_resume:
        spec = _open_value(db, entry, account) or capture.next_value(
            _values(db, entry, account), _recorded(db, entry)
        )
        if spec:
            next_capture = capture.spec_out(spec)

    support_note = None
    if entry.status == "completed" and entry.crisis_tier == "mild":
        support_note = crisis.mild_reference()

    out = EntryOut.model_validate(entry, from_attributes=True).model_dump(
        exclude={"messages", "next_capture", "support_note", "pending_notice", "pending_resume"}
    )
    return EntryStepOut(
        **out,
        support_note=support_note,
        pending_notice=pending_notice,
        pending_resume=pending_resume,
        next_capture=next_capture,
        messages=_messages(db, entry),
        crisis_event=crisis_event.as_dict() if crisis_event else None,
        referral=referral,
    )


def _screen(
    db: Session,
    account: Account,
    entry: Entry,
    field: str,
    text: str,
    history: list[tuple[str, str]],
) -> tuple[crisis.CrisisResult | None, llm_client.Classification]:
    """
    FR-CRIS-001/002: screen one free-text value before it reaches any generated
    output. The model stage is skipped once the entry is suppressed — no
    generative request may be issued for it (FR-CRIS-006).
    """
    if crisis.suppressed(entry):
        classification = llm_client.Classification(domains=None, risk_tier=None)
    else:
        classification = llm_client.classify(history, text, account.work_issues)

    tier = crisis.assess(text, classification.risk_tier)
    result = crisis.record(db, account.id, entry, field, tier)
    if result and result.text:
        _add_message(db, entry, "ai", "crisis", result.text)
    return result, classification


def _ask_next(db: Session, entry: Entry, account: Account, previous_answer: str | None, resuming: bool = False) -> None:
    """
    Adds the next capture prompt, or — when every value is recorded — completes
    the entry and adds the conversation offer.
    """
    journal_type = _journal_type(entry)
    focus_codes = _focus_codes(db, account)
    values = capture.values_for(journal_type, focus_codes)
    recorded = _recorded(db, entry)
    spec = capture.next_value(values, recorded)

    if spec is None:
        # FR-ENT-020: complete on the final capture, independent of the conversation.
        entry.status = "completed"
        entry.completed_at = datetime.utcnow()
        if crisis.suppressed(entry):
            entry.conversation_status = "suppressed"
            _add_message(db, entry, "ai", "notice", capture.FALLBACKS["saved"])
        else:
            ending = capture.ending_for(journal_type, focus_codes)
            if ending == "close":
                # FR-JRN-002: one closing message, no further value, no conversation.
                entry.conversation_status = "closed"
                entry.closure_reason = "journal_complete"
                _add_message(db, entry, "ai", "closing", _closing_text(db, entry, account, values, recorded))
            elif ending == "grounding":
                # FR-JRN-005: the trauma variant ends here, without a conversation.
                entry.conversation_status = "closed"
                entry.closure_reason = "journal_complete"
            else:
                entry.conversation_status = "offered"
                _add_message(db, entry, "ai", "offer", capture.FALLBACKS["offer"])
        _maybe_ground(db, entry, journal_type, focus_codes)
        return

    if not resuming and _pause_due(values, recorded, spec):
        # FR-JRN-006: the rest of this entry belongs to after she has done it.
        _add_message(db, entry, "ai", "pause", capture.pause_message())
        return

    result = None
    if not crisis.suppressed(entry):
        prompt = context_builder.build_capture_prompt(
            _focus_codes(db, account),
            journal_type,
            capture.summary(values, recorded, _user_terms(db, account)),
            spec["id"],
            previous_answer,
            reply_language=language.reply_language(_free_text_answers(values, recorded)),
        )
        result = llm_client.generate_capture_prompt(prompt, spec["id"])

    text = result.message_text if result else capture.fallback_wording(journal_type, spec["id"])
    _add_message(db, entry, "ai", "capture_prompt", text, value_id=spec["id"])


def _maybe_ground(db: Session, entry: Entry, journal_type: str, focus_codes: list[str]) -> None:
    """
    FR-JRN-008: for users with the past-event focus area, the thread's final
    message is the fixed grounding message. Added once, when the thread ends.
    """
    if entry.conversation_status not in ("closed", "declined", "suppressed"):
        return
    if not capture.needs_grounding(journal_type, focus_codes):
        return
    if db.query(Message).filter(Message.entry_id == entry.id, Message.kind == "grounding").count():
        return
    _add_message(db, entry, "ai", "grounding", capture.grounding_message())


def _closing_text(db: Session, entry: Entry, account: Account, values: list[dict], recorded: dict) -> str:
    journal_type = _journal_type(entry)
    prompt = context_builder.build_closing_prompt(
        _focus_codes(db, account),
        journal_type,
        capture.summary(values, recorded, _user_terms(db, account)),
        reply_language=language.reply_language(_free_text_answers(values, recorded)),
        skills=capture.JOURNAL_SKILLS.get(journal_type),
    )
    text = llm_client.generate_closing(prompt)
    if text:
        # FR-AIR-011: a closing message carries no question.
        text = enforcement.limit_questions(text, allowed=0)
    return text or capture.FALLBACKS[f"{journal_type}_close"]


def _conversation_history(messages: list[Message]) -> list[tuple[str, str]]:
    """Only the conversation after the offer; capture Q&A reaches the model as the entry summary."""
    offer_seq = next((m.sequence for m in messages if m.kind == "offer"), None)
    if offer_seq is None:
        return []
    return [
        (m.role, m.content)
        for m in messages
        if m.sequence > offer_seq and m.kind == "chat"
    ]


def _reflect(
    db: Session,
    entry: Entry,
    account: Account,
    history: list[tuple[str, str]],
    text: str | None,
    domains: list[str] | None,
) -> enforcement.Outcome:
    """Context → reflection → enforcement. Raises LLMError."""
    journal_type = _journal_type(entry)
    values = _values(db, entry, account)
    recorded = _recorded(db, entry)
    summary = capture.summary(values, recorded, _user_terms(db, account))

    # Language of the latest thing she wrote; capture answers count for Echo's first message.
    user_texts = _free_text_answers(values, recorded) + [c for role, c in history if role == "user"]
    if text is not None:
        user_texts.append(text)

    domains = domains or entry.domains or ["general"]
    entry.domains = domains

    # FR-AIR-009/013: how long this journal's conversation may run. Free write is
    # an extended session, so it circles and pauses further before closing.
    limits = capture.conversation_limits(journal_type)
    close_reason = enforcement.required_closure(
        history,
        text,
        limits["containment_turns"] or settings.CONVERSATION_CONTAINMENT_TURNS,
        entry.conversation_stage,
        minimal_replies=limits["minimal_replies"],
    )
    system_prompt = context_builder.build_system_prompt(
        account,
        _focus_codes(db, account),
        domains,
        entry.journal_type,
        summary,
        close_reason=close_reason,
        reply_language=language.reply_language(user_texts),
        repeat_limit=limits["repeat_limit"],
    )
    reflection = llm_client.generate_reflection(system_prompt, history, text)
    outcome = enforcement.enforce(reflection, forced_closure=close_reason, repeat_limit=limits["repeat_limit"])
    # One consolidating turn after she moves, then the closing message.
    entry.conversation_stage = enforcement.CONFIRMING if outcome.shift_noticed else enforcement.EXPLORING
    return outcome


def _suppress_conversation(db: Session, entry: Entry, account: Account) -> None:
    entry.conversation_status = "suppressed"
    entry.closure_reason = "crisis"
    _maybe_ground(db, entry, _journal_type(entry), _focus_codes(db, account))


def _apply_close(db: Session, entry: Entry, account: Account, outcome: enforcement.Outcome) -> None:
    if outcome.session_end:
        entry.conversation_status = "closed"
        entry.closure_reason = outcome.closure_reason
        _maybe_ground(db, entry, _journal_type(entry), _focus_codes(db, account))


def _schedule_values(entry: Entry) -> list[dict]:
    """The base schedule, enough to know which values are free text."""
    journal_type = context_builder.normalise_journal_type(entry.journal_type)
    return capture.SCHEDULES.get(journal_type, [])


def _cycle_number(db: Session, entry: Entry) -> int | None:
    """FR-JRN-006: 1 for a first exposure, 2 for the cycle that continues it, and so on."""
    if not entry.parent_entry_id:
        return 1 if entry.journal_type == "exposure" else None
    number, parent_id, seen = 1, entry.parent_entry_id, set()
    while parent_id and parent_id not in seen:
        seen.add(parent_id)
        number += 1
        parent_id = db.query(Entry.parent_entry_id).filter(Entry.id == parent_id).scalar()
    return number


def _summary(db: Session, entry: Entry) -> EntrySummary:
    last = db.query(Message).filter(Message.entry_id == entry.id).order_by(Message.sequence.desc()).first()
    recorded = _recorded(db, entry)
    mood = recorded.get("mood")
    # FR-ENT-009: her name for the entry, or a date-based one when she skipped it.
    name = recorded.get("name") if "name" in recorded else None
    if name is None and "name" in {v["id"] for v in capture.SCHEDULES.get(_journal_type(entry), [])}:
        name = f"{_journal_type(entry).replace('_', ' ').title()} — {entry.started_at:%d %b %Y}"
    preview = next((t for t in _free_text_answers(_schedule_values(entry), recorded)), None)
    if preview is None:
        first_said = db.query(Message).filter(
            Message.entry_id == entry.id, Message.role == "user"
        ).order_by(Message.sequence).first()
        preview = first_said.content if first_said else None

    return EntrySummary(
        id=entry.id,
        journal_type=entry.journal_type,
        status=entry.status,
        started_at=entry.started_at,
        completed_at=entry.completed_at,
        last_activity_at=last.created_at if last else entry.started_at,
        mood=mood,
        name=name,
        cycle=_cycle_number(db, entry),
        preview=(preview[:120] + "…") if preview and len(preview) > 120 else preview,
    )


# ---------- endpoints ----------

@router.get("", response_model=list[EntrySummary])
def list_entries(
    status_: str = Query("all", alias="status", pattern="^(all|completed|in_progress)$"),
    since_days: int | None = Query(None, ge=1, le=3650),
    q: str | None = Query(None, min_length=1, max_length=100),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    account: Account = Depends(get_current_user),
):
    """
    FR-ENT-006: the user's entries, most recently touched first. Drafts are
    included by default so nothing she wrote is hidden; status=in_progress
    &since_days=7 gives the drafts the dashboard offers to resume (FR-HOME-002).
    q searches what she wrote: her answers and every message of the thread.
    """
    query = db.query(Entry).filter(Entry.account_id == account.id)
    if status_ != "all":
        query = query.filter(Entry.status == status_)
    if q:
        like = f"%{q}%"
        query = query.filter(or_(
            Entry.id.in_(select(Message.entry_id).where(Message.content.ilike(like))),
            Entry.id.in_(select(CapturedValue.entry_id).where(CapturedValue.value.ilike(like))),
        ))

    summaries = [_summary(db, e) for e in query.order_by(Entry.started_at.desc()).limit(200).all()]
    if since_days:
        cutoff = datetime.utcnow() - timedelta(days=since_days)
        summaries = [s for s in summaries if s.last_activity_at >= cutoff]
    summaries.sort(key=lambda s: s.last_activity_at, reverse=True)
    return summaries[:limit]


@router.post("", response_model=EntryStepOut, status_code=status.HTTP_201_CREATED)
def create_entry(
    payload: EntryCreate,
    db: Session = Depends(get_db),
    account: Account = Depends(get_current_user),
):
    journal_type = context_builder.normalise_journal_type(payload.journal_type)
    if not (context_builder.is_supported_journal_type(journal_type) and capture.has_schedule(journal_type)):
        raise HTTPException(status_code=422, detail="Unsupported journal type")

    parent = None
    if payload.parent_entry_id:
        # FR-JRN-006: a further cycle of a completed exposure, reusing its feared outcome.
        parent = _get_entry(db, payload.parent_entry_id, account)
        if parent.journal_type != journal_type or parent.status != "completed":
            raise HTTPException(status_code=409, detail="Can only continue a completed cycle of the same journal")

    entry = Entry(
        account_id=account.id,
        journal_type=journal_type,
        status="in_progress",
        parent_entry_id=parent.id if parent else None,
        # The notice was already shown and acknowledged in the cycle this one continues.
        notice_acknowledged=bool(parent),
    )
    db.add(entry)
    db.flush()

    if parent:
        carried = _recorded(db, parent)
        values = capture.values_for(journal_type, _focus_codes(db, account))
        for value_id in capture.carried_values(journal_type):
            if value_id not in carried or carried[value_id] is None:
                continue
            spec = next(v for v in values if v["id"] == value_id)
            db.add(CapturedValue(
                entry_id=entry.id,
                key=value_id,
                value=json.dumps(carried[value_id], ensure_ascii=False),
                skipped=False,
            ))
            _add_message(db, entry, "user", "capture_answer",
                         capture.display_text(spec, carried[value_id]), value_id=value_id)
        db.flush()

    # FR-JRN-007: the notice comes first; the first value waits for the acknowledgement.
    notice = capture.notice_for(journal_type, _focus_codes(db, account))
    if notice and not entry.notice_acknowledged:
        _add_message(db, entry, "ai", "notice", notice)
    else:
        _ask_next(db, entry, account, previous_answer=None)
    db.commit()
    db.refresh(entry)
    return _state(db, entry, account)


@router.post("/{entry_id}/resume", response_model=EntryStepOut)
def resume_entry(
    entry_id: uuid.UUID,
    db: Session = Depends(get_db),
    account: Account = Depends(get_current_user),
):
    """
    FR-JRN-006: she has done the thing she planned and is back to record how it
    went. Until she says so, the entry asks nothing — it may be hours or days.
    """
    entry = _get_entry(db, entry_id, account)
    if not _paused(db, entry):
        raise HTTPException(status_code=409, detail="This entry is not waiting to be resumed")
    _ask_next(db, entry, account, previous_answer=None, resuming=True)
    db.commit()
    db.refresh(entry)
    return _state(db, entry, account)


@router.post("/{entry_id}/acknowledge", response_model=EntryStepOut)
def acknowledge_notice(
    entry_id: uuid.UUID,
    db: Session = Depends(get_db),
    account: Account = Depends(get_current_user),
):
    """FR-JRN-007: the user confirms she has read the scope notice; capture starts."""
    entry = _get_entry(db, entry_id, account)
    if entry.notice_acknowledged:
        raise HTTPException(status_code=409, detail="Already acknowledged")
    entry.notice_acknowledged = True
    _ask_next(db, entry, account, previous_answer=None)
    db.commit()
    db.refresh(entry)
    return _state(db, entry, account)


@router.get("/{entry_id}", response_model=EntryStepOut)
def get_entry(
    entry_id: uuid.UUID,
    db: Session = Depends(get_db),
    account: Account = Depends(get_current_user),
):
    return _state(db, _get_entry(db, entry_id, account), account)


@router.delete("/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_entry(
    entry_id: uuid.UUID,
    db: Session = Depends(get_db),
    account: Account = Depends(get_current_user),
):
    """FR-ENT-008: permanent. Deleted explicitly as well as by FK cascade, so nothing is left behind."""
    entry = _get_entry(db, entry_id, account)
    db.query(Message).filter(Message.entry_id == entry.id).delete(synchronize_session=False)
    db.query(CapturedValue).filter(CapturedValue.entry_id == entry.id).delete(synchronize_session=False)
    db.query(CrisisEvent).filter(CrisisEvent.entry_id == entry.id).update(
        {CrisisEvent.entry_id: None}, synchronize_session=False
    )
    db.delete(entry)
    db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{entry_id}/captures", response_model=EntryStepOut)
def submit_capture(
    entry_id: uuid.UUID,
    payload: CaptureAnswer,
    db: Session = Depends(get_db),
    account: Account = Depends(get_current_user),
):
    entry = _get_entry(db, entry_id, account)
    journal_type = _journal_type(entry)
    if entry.status != "in_progress" or not capture.has_schedule(journal_type):
        raise HTTPException(status_code=409, detail="This entry is not collecting values")
    # FR-JRN-007: nothing is collected until the scope notice is acknowledged.
    if capture.notice_for(journal_type, _focus_codes(db, account)) and not entry.notice_acknowledged:
        raise HTTPException(status_code=409, detail="The notice must be acknowledged first")
    # FR-JRN-006: nor while the entry waits for her to go and do what she planned.
    if _paused(db, entry):
        raise HTTPException(status_code=409, detail="This entry is waiting until you have done it")

    # FR-ENT-024: only the next unrecorded value can be answered, exactly once.
    # A repeatable value (free write) stays open across several messages, so it
    # is "unrecorded" until she says she is done.
    open_value = _open_value(db, entry, account)
    spec = open_value or capture.next_value(_values(db, entry, account), _recorded(db, entry))
    if spec is None or spec["id"] != payload.value_id:
        raise HTTPException(status_code=409, detail=f"Expected value: {spec['id'] if spec else 'none'}")

    extra = _user_terms(db, account)
    row = _value_row(db, entry, spec["id"]) if spec.get("repeatable") else None
    finishing = bool(spec.get("repeatable")) and not payload.more

    if row is not None and finishing and not payload.value:
        # "Done writing" on what she has already sent: nothing new to record.
        value, shown = json.loads(row.value), None
    else:
        try:
            value = capture.validate(spec, payload.value, payload.skipped, extra)
        except capture.InvalidAnswer as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        shown = capture.display_text(spec, value, extra)

    # FR-ENT-003 / FR-ENT-005: persist the answer before crisis detection or any AI call.
    if shown is not None:
        if row is not None and value is not None:
            # Same value, another message: keep them together as one account.
            value = f"{json.loads(row.value)}\n\n{value}"
            row.value = json.dumps(value, ensure_ascii=False)
        else:
            db.add(CapturedValue(
                entry_id=entry.id,
                key=spec["id"],
                value=None if value is None else json.dumps(value, ensure_ascii=False),
                skipped=value is None,
            ))
        _add_message(db, entry, "user", "capture_answer", shown, value_id=spec["id"])
    db.commit()

    event = None
    if shown is not None and spec["control"] == "free_text" and value is not None:
        event, _ = _screen(db, account, entry, spec["id"], shown, history=[])

    if spec.get("repeatable") and payload.more:
        # She is still writing: no new prompt, the same value stays open.
        db.commit()
        db.refresh(entry)
        return _state(db, entry, account, crisis_event=event)

    _ask_next(db, entry, account, previous_answer=f"{spec['id']} = {shown or 'written'}")
    db.commit()
    db.refresh(entry)
    return _state(db, entry, account, crisis_event=event)


@router.post("/{entry_id}/conversation", response_model=EntryStepOut)
def conversation(
    entry_id: uuid.UUID,
    payload: ConversationChoice,
    db: Session = Depends(get_db),
    account: Account = Depends(get_current_user),
):
    entry = _get_entry(db, entry_id, account)

    if payload.choice == "end":
        if entry.conversation_status != "active":
            raise HTTPException(status_code=409, detail="No active conversation")
        entry.conversation_status = "closed"
        entry.closure_reason = "user_ended"
        _maybe_ground(db, entry, _journal_type(entry), _focus_codes(db, account))
        db.commit()
        db.refresh(entry)
        return _state(db, entry, account)

    if entry.conversation_status != "offered" or crisis.suppressed(entry):
        raise HTTPException(status_code=409, detail="No conversation offer is open")

    if payload.choice == "stop":
        entry.conversation_status = "declined"
        _maybe_ground(db, entry, _journal_type(entry), _focus_codes(db, account))
        db.commit()
        db.refresh(entry)
        return _state(db, entry, account)

    # continue: Echo speaks first, referring to what was recorded (FR-AIR-002).
    summary = capture.summary(_values(db, entry, account), _recorded(db, entry), _user_terms(db, account))
    domains = llm_client.classify([], summary, account.work_issues).domains
    try:
        outcome = _reflect(db, entry, account, history=[], text=None, domains=domains)
    except llm_client.LLMError:
        db.rollback()
        raise HTTPException(status_code=503, detail="Reflection is unavailable right now, please try again")

    entry.conversation_status = "active"
    event = None
    if outcome.crisis:
        event = crisis.record(db, account.id, entry, "model", "danger")
        _add_message(db, entry, "ai", "crisis", event.text)
        _suppress_conversation(db, entry, account)
    else:
        _add_message(db, entry, "ai", "chat", outcome.reply_text)
        _apply_close(db, entry, account, outcome)
    db.commit()
    db.refresh(entry)
    return _state(db, entry, account, crisis_event=event, referral=outcome.referral)


@router.post("/{entry_id}/messages", response_model=EntryStepOut, status_code=status.HTTP_201_CREATED)
def add_message(
    entry_id: uuid.UUID,
    payload: MessageCreate,
    db: Session = Depends(get_db),
    account: Account = Depends(get_current_user),
):
    """
    One conversation turn: persist → crisis screen (lexical + model) →
    context → reflection → enforcement → save reply.
    """
    entry = _get_entry(db, entry_id, account)
    if entry.conversation_status != "active" or crisis.suppressed(entry):
        raise HTTPException(status_code=409, detail="No active conversation")

    history = _conversation_history(_messages(db, entry))
    text = payload.content.strip()

    # FR-ENT-026: the user's message is stored before anything else can fail.
    _add_message(db, entry, "user", "chat", text)
    db.commit()

    event, classification = _screen(db, account, entry, "chat", text, history)
    if crisis.suppressed(entry):
        # FR-CRIS-006/007: no further generation for this entry.
        _suppress_conversation(db, entry, account)
        db.commit()
        db.refresh(entry)
        return _state(db, entry, account, crisis_event=event, referral=True)

    if payload.more:
        # FR-JRN-003 in the conversation: she is still writing. The message is
        # stored and screened; Echo waits and answers all of it at once, because
        # interrupting someone mid-thought is the opposite of listening.
        db.commit()
        db.refresh(entry)
        return _state(db, entry, account, crisis_event=event)

    try:
        outcome = _reflect(db, entry, account, history, text, classification.domains)
    except llm_client.LLMError:
        _add_message(db, entry, "ai", "notice", REPLY_UNAVAILABLE)
        db.commit()
        db.refresh(entry)
        return _state(db, entry, account, crisis_event=event)

    if outcome.crisis:
        # The model noticed risk the rules missed: its reply is replaced by fixed content.
        event = crisis.record(db, account.id, entry, "model", "danger")
        _add_message(db, entry, "ai", "crisis", event.text)
        _suppress_conversation(db, entry, account)
    else:
        _add_message(db, entry, "ai", "chat", outcome.reply_text)
        _apply_close(db, entry, account, outcome)
    db.commit()
    db.refresh(entry)
    return _state(db, entry, account, crisis_event=event, referral=outcome.referral)
