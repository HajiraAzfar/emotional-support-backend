"""
Entry engine.

Flow for a journal type with a capture schedule (check_in, free_write, savouring):
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


def _free_text_answers(journal_type: str, recorded: dict) -> list[str]:
    """What the user wrote in her own words during capture — the best hint of her language."""
    return [
        recorded[spec["id"]]
        for spec in capture.SCHEDULES.get(journal_type, [])
        if spec["control"] == "free_text" and recorded.get(spec["id"])
    ]


def _journal_type(entry: Entry) -> str:
    return context_builder.normalise_journal_type(entry.journal_type)


def _state(db: Session, entry: Entry, crisis_event: crisis.CrisisResult | None = None, referral: bool = False) -> EntryStepOut:
    next_capture = None
    journal_type = _journal_type(entry)
    if entry.status == "in_progress" and capture.has_schedule(journal_type):
        spec = capture.next_value(journal_type, set(_recorded(db, entry)))
        if spec:
            next_capture = capture.spec_out(spec)

    support_note = None
    if entry.status == "completed" and entry.crisis_tier == "mild":
        support_note = crisis.mild_reference()

    out = EntryOut.model_validate(entry, from_attributes=True).model_dump(
        exclude={"messages", "next_capture", "support_note"}
    )
    return EntryStepOut(
        **out,
        support_note=support_note,
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


def _ask_next(db: Session, entry: Entry, account: Account, previous_answer: str | None) -> None:
    """
    Adds the next capture prompt, or — when every value is recorded — completes
    the entry and adds the conversation offer.
    """
    journal_type = _journal_type(entry)
    recorded = _recorded(db, entry)
    spec = capture.next_value(journal_type, set(recorded))

    if spec is None:
        # FR-ENT-020: complete on the final capture, independent of the conversation.
        entry.status = "completed"
        entry.completed_at = datetime.utcnow()
        if crisis.suppressed(entry):
            entry.conversation_status = "suppressed"
            _add_message(db, entry, "ai", "notice", capture.FALLBACKS["saved"])
        elif capture.ENDINGS.get(journal_type) == "close":
            # FR-JRN-002: one closing message, no further value, no conversation.
            entry.conversation_status = "closed"
            entry.closure_reason = "journal_complete"
            _add_message(db, entry, "ai", "closing", _closing_text(db, entry, account, journal_type, recorded))
        else:
            entry.conversation_status = "offered"
            _add_message(db, entry, "ai", "offer", capture.FALLBACKS["offer"])
        return

    result = None
    if not crisis.suppressed(entry):
        prompt = context_builder.build_capture_prompt(
            _focus_codes(db, account),
            journal_type,
            capture.summary(journal_type, recorded, _user_terms(db, account)),
            spec["id"],
            previous_answer,
            reply_language=language.reply_language(_free_text_answers(journal_type, recorded)),
        )
        result = llm_client.generate_capture_prompt(prompt, spec["id"])

    text = result.message_text if result else capture.fallback_wording(journal_type, spec["id"])
    _add_message(db, entry, "ai", "capture_prompt", text, value_id=spec["id"])


def _closing_text(db: Session, entry: Entry, account: Account, journal_type: str, recorded: dict) -> str:
    prompt = context_builder.build_closing_prompt(
        _focus_codes(db, account),
        journal_type,
        capture.summary(journal_type, recorded, _user_terms(db, account)),
        reply_language=language.reply_language(_free_text_answers(journal_type, recorded)),
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
    recorded = _recorded(db, entry)
    summary = capture.summary(journal_type, recorded, _user_terms(db, account))

    # Language of the latest thing she wrote; capture answers count for Echo's first message.
    user_texts = _free_text_answers(journal_type, recorded) + [c for role, c in history if role == "user"]
    if text is not None:
        user_texts.append(text)

    domains = domains or entry.domains or ["general"]
    entry.domains = domains

    close_reason = enforcement.required_closure(history, text, settings.CONVERSATION_CONTAINMENT_TURNS)
    system_prompt = context_builder.build_system_prompt(
        account,
        _focus_codes(db, account),
        domains,
        entry.journal_type,
        summary,
        close_reason=close_reason,
        reply_language=language.reply_language(user_texts),
    )
    reflection = llm_client.generate_reflection(system_prompt, history, text)
    return enforcement.enforce(reflection, forced_closure=close_reason)


def _suppress_conversation(entry: Entry) -> None:
    entry.conversation_status = "suppressed"
    entry.closure_reason = "crisis"


def _apply_close(entry: Entry, outcome: enforcement.Outcome) -> None:
    if outcome.session_end:
        entry.conversation_status = "closed"
        entry.closure_reason = outcome.closure_reason


def _summary(db: Session, entry: Entry) -> EntrySummary:
    last = db.query(Message).filter(Message.entry_id == entry.id).order_by(Message.sequence.desc()).first()
    mood_row = db.query(CapturedValue).filter(
        CapturedValue.entry_id == entry.id, CapturedValue.key == "mood"
    ).first()
    mood = json.loads(mood_row.value) if mood_row and mood_row.value else None
    return EntrySummary(
        id=entry.id,
        journal_type=entry.journal_type,
        status=entry.status,
        started_at=entry.started_at,
        completed_at=entry.completed_at,
        last_activity_at=last.created_at if last else entry.started_at,
        mood=mood,
    )


# ---------- endpoints ----------

@router.get("", response_model=list[EntrySummary])
def list_entries(
    status_: str = Query("completed", alias="status", pattern="^(completed|in_progress)$"),
    since_days: int | None = Query(None, ge=1, le=3650),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    account: Account = Depends(get_current_user),
):
    """
    FR-ENT-006: completed entries, newest first. status=in_progress&since_days=7
    gives the drafts the dashboard offers to resume (FR-HOME-002).
    """
    query = db.query(Entry).filter(Entry.account_id == account.id, Entry.status == status_)
    order = Entry.completed_at.desc() if status_ == "completed" else Entry.started_at.desc()
    summaries = [_summary(db, e) for e in query.order_by(order).limit(limit).all()]

    if since_days:
        cutoff = datetime.utcnow() - timedelta(days=since_days)
        summaries = [s for s in summaries if s.last_activity_at >= cutoff]
    if status_ == "in_progress":
        summaries.sort(key=lambda s: s.last_activity_at, reverse=True)
    return summaries


@router.post("", response_model=EntryStepOut, status_code=status.HTTP_201_CREATED)
def create_entry(
    payload: EntryCreate,
    db: Session = Depends(get_db),
    account: Account = Depends(get_current_user),
):
    journal_type = context_builder.normalise_journal_type(payload.journal_type)
    if not (context_builder.is_supported_journal_type(journal_type) and capture.has_schedule(journal_type)):
        raise HTTPException(status_code=422, detail="Unsupported journal type")

    entry = Entry(account_id=account.id, journal_type=journal_type, status="in_progress")
    db.add(entry)
    db.flush()
    _ask_next(db, entry, account, previous_answer=None)
    db.commit()
    db.refresh(entry)
    return _state(db, entry)


@router.get("/{entry_id}", response_model=EntryStepOut)
def get_entry(
    entry_id: uuid.UUID,
    db: Session = Depends(get_db),
    account: Account = Depends(get_current_user),
):
    return _state(db, _get_entry(db, entry_id, account))


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

    # FR-ENT-024: only the next unrecorded value can be answered, exactly once.
    spec = capture.next_value(journal_type, set(_recorded(db, entry)))
    if spec is None or spec["id"] != payload.value_id:
        raise HTTPException(status_code=409, detail=f"Expected value: {spec['id'] if spec else 'none'}")

    extra = _user_terms(db, account)
    try:
        value = capture.validate(spec, payload.value, payload.skipped, extra)
    except capture.InvalidAnswer as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    # FR-ENT-003 / FR-ENT-005: persist the answer before crisis detection or any AI call.
    shown = capture.display_text(spec, value, extra)
    db.add(CapturedValue(
        entry_id=entry.id,
        key=spec["id"],
        value=None if value is None else json.dumps(value, ensure_ascii=False),
        skipped=value is None,
    ))
    _add_message(db, entry, "user", "capture_answer", shown, value_id=spec["id"])
    db.commit()

    event = None
    if spec["control"] == "free_text" and value is not None:
        event, _ = _screen(db, account, entry, spec["id"], value, history=[])

    _ask_next(db, entry, account, previous_answer=f"{spec['id']} = {shown}")
    db.commit()
    db.refresh(entry)
    return _state(db, entry, crisis_event=event)


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
        db.commit()
        db.refresh(entry)
        return _state(db, entry)

    if entry.conversation_status != "offered" or crisis.suppressed(entry):
        raise HTTPException(status_code=409, detail="No conversation offer is open")

    if payload.choice == "stop":
        entry.conversation_status = "declined"
        db.commit()
        db.refresh(entry)
        return _state(db, entry)

    # continue: Echo speaks first, referring to what was recorded (FR-AIR-002).
    summary = capture.summary(_journal_type(entry), _recorded(db, entry), _user_terms(db, account))
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
        _suppress_conversation(entry)
    else:
        _add_message(db, entry, "ai", "chat", outcome.reply_text)
        _apply_close(entry, outcome)
    db.commit()
    db.refresh(entry)
    return _state(db, entry, crisis_event=event, referral=outcome.referral)


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
        _suppress_conversation(entry)
        db.commit()
        db.refresh(entry)
        return _state(db, entry, crisis_event=event, referral=True)

    try:
        outcome = _reflect(db, entry, account, history, text, classification.domains)
    except llm_client.LLMError:
        _add_message(db, entry, "ai", "notice", REPLY_UNAVAILABLE)
        db.commit()
        db.refresh(entry)
        return _state(db, entry, crisis_event=event)

    if outcome.crisis:
        # The model noticed risk the rules missed: its reply is replaced by fixed content.
        event = crisis.record(db, account.id, entry, "model", "danger")
        _add_message(db, entry, "ai", "crisis", event.text)
        _suppress_conversation(entry)
    else:
        _add_message(db, entry, "ai", "chat", outcome.reply_text)
        _apply_close(entry, outcome)
    db.commit()
    db.refresh(entry)
    return _state(db, entry, crisis_event=event, referral=outcome.referral)
