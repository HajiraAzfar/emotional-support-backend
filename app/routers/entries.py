"""
Entry engine.

AI Chat (journal type "chat") has no capture: the conversation is open from
creation, and POST /entries/{id}/messages is the whole flow.

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
output. In a journal, a danger or emergency tier stops every AI call for that
entry; the entry itself can still be completed (FR-CRIS-004, 006, 007). AI Chat
keeps talking: the safety check (crisis.triage) runs before the skill is chosen,
and a helplines card (the reply's own `card` field) goes under Echo's reply, never
instead of it and never in its text.
"""
import json
import logging
import re
import uuid
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.core import capture, context_builder, crisis, enforcement, language, llm_client, md_loader
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
    CheckInCreate,
    CheckInOut,
    ConversationChoice,
    EntryCreate,
    EntryOut,
    EntryStepOut,
    EntrySummary,
)
from app.schemas.message import MessageCreate

router = APIRouter(prefix="/entries", tags=["entries"])
logger = logging.getLogger(__name__)

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


def _add_message(
    db: Session, entry: Entry, role: str, kind: str, content: str,
    value_id: str | None = None, card: str | None = None, client_id: uuid.UUID | None = None,
) -> Message:
    # The entry row is locked for the rest of the transaction, so two requests
    # can't take the same position (positions are unique per thread).
    db.query(Entry.id).filter(Entry.id == entry.id).with_for_update().first()
    last = db.query(func.max(Message.sequence)).filter(Message.entry_id == entry.id).scalar() or 0
    message = Message(
        entry_id=entry.id,
        role=role,
        kind=kind,
        value_id=value_id,
        content=content,
        card=card,
        sequence=last + 1,
        client_id=client_id,
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
    # AI Chat shows help through the card rules instead (crisis.card_for).
    if entry.status == "completed" and entry.crisis_tier == "mild" and entry.journal_type != context_builder.CHAT:
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
    lang: str = language.ENGLISH,
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
    result = crisis.record(db, account.id, entry, field, tier, lang)
    # AI Chat puts no fixed text in the thread: the card goes under Echo's own reply.
    if result and result.text and _journal_type(entry) != context_builder.CHAT:
        _add_message(db, entry, "ai", "crisis", result.text)
    return result, classification


def _screen_journal(
    db: Session,
    account: Account,
    entry: Entry,
    field: str,
    text: str,
    history: list[tuple[str, str]],
    lang: str = language.ENGLISH,
) -> tuple[crisis.CrisisResult | None, llm_client.Classification, str | None]:
    """
    Journals only (AI Chat uses _screen): screen free text before anything else
    reads it. The classifier may read it; the tier then follows the journal rules
    (crisis.journal_tier). Danger and emergency add fixed content and stop every
    generation call for the entry (crisis.suppressed). Returns the event, the
    classification and a helplines card for the next message, if any.
    """
    if crisis.suppressed(entry):
        classification = llm_client.Classification(domains=None, risk_tier=None)
    else:
        classification = llm_client.classify(history, text, account.work_issues)
    tier, card = crisis.journal_tier(text, classification)
    result = crisis.record(db, account.id, entry, field, tier, lang)
    if result and result.text:
        _add_message(db, entry, "ai", "crisis", result.text)
    return result, classification, card


def _card_due(db: Session, entry: Entry, safety, level: str | None) -> str | None:
    """
    AI Chat: the helplines card for this reply (soft | prominent), or None. Shown once;
    again only when she asks for help or it becomes prominent (rules: crisis.card_for).
    """
    card = crisis.card_for(level, safety)
    if card is None:
        return None
    last = db.query(Message.card).filter(
        Message.entry_id == entry.id, Message.card.isnot(None)
    ).order_by(Message.sequence.desc()).first()
    if last is None or (safety is not None and safety.asks_for_help) or (card == "prominent" and last.card == "soft"):
        return card
    return None


def _ask_next(
    db: Session, entry: Entry, account: Account, previous_answer: str | None,
    resuming: bool = False, card: str | None = None,
) -> None:
    """
    Adds the next capture prompt, or — when every value is recorded — completes
    the entry and adds the conversation offer. `card`: a helplines card to draw
    under that next message (a disclosure in the answer just given).
    """
    journal_type = _journal_type(entry)
    focus_codes = _focus_codes(db, account)
    values = capture.values_for(journal_type, focus_codes)
    recorded = _recorded(db, entry)
    # Values the conditions passed over are stored as not asked, never as skipped.
    for value_id in capture.not_asked(values, recorded):
        db.add(CapturedValue(entry_id=entry.id, key=value_id, value=None, skipped=False, status="not_asked"))
        recorded[value_id] = None
    db.flush()
    spec = capture.next_value(values, recorded)

    if spec is None:
        # FR-ENT-020: complete on the final capture, independent of the conversation.
        entry.status = "completed"
        entry.completed_at = datetime.utcnow()
        if crisis.suppressed(entry):
            entry.conversation_status = "suppressed"
            _add_message(db, entry, "ai", "notice", capture.FALLBACKS["saved"], card=card)
        else:
            ending = capture.ending_for(journal_type, focus_codes)
            if ending == "close":
                # FR-JRN-002: one closing message, no further value, no conversation.
                entry.conversation_status = "closed"
                entry.closure_reason = "journal_complete"
                _add_message(db, entry, "ai", "closing", _closing_text(db, entry, account, values, recorded), card=card)
            elif ending == "grounding":
                # FR-JRN-005: the trauma variant ends here, without a conversation.
                entry.conversation_status = "closed"
                entry.closure_reason = "journal_complete"
            else:
                entry.conversation_status = "offered"
                _add_message(db, entry, "ai", "offer", capture.FALLBACKS["offer"], card=card)
        _maybe_ground(db, entry, journal_type, focus_codes, card=card)
        return

    if not resuming and _pause_due(values, recorded, spec):
        # FR-JRN-006: the rest of this entry belongs to after she has done it.
        _add_message(db, entry, "ai", "pause", capture.pause_message(), card=card)
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

    # The control comes from the schedule; the words must carry nothing internal.
    text = enforcement.strip_card_labels(result.message_text) if result else ""
    if not text or _leaks(text):
        text = capture.fallback_wording(journal_type, spec["id"])
    _add_message(db, entry, "ai", "capture_prompt", text, value_id=spec["id"], card=card)


# Words that only exist inside the app: value ids, empty-answer labels, tags.
_INTERNAL = re.compile(
    r"\b(" + "|".join(re.escape(v) for v in capture.all_value_ids() if "_" in v)
    + r"|triggers?|skipped|skip|not_asked|value_id|referral_flag)\b|</?user_text>", re.I)


def _leaks(text: str) -> bool:
    return bool(_INTERNAL.search(text))


def _maybe_ground(db: Session, entry: Entry, journal_type: str, focus_codes: list[str], card: str | None = None) -> None:
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
    _add_message(db, entry, "ai", "grounding", capture.grounding_message(), card=card)


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
    """
    Only the conversation after the offer; capture Q&A reaches the model as the
    entry summary. An AI Chat has no offer: all of it is conversation.
    """
    offer_seq = next((m.sequence for m in messages if m.kind == "offer"), 0)
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


def _chat_reply(
    db: Session,
    entry: Entry,
    account: Account,
    history: list[tuple[str, str]],
    text: str,
    domains: list[str] | None,
    safety=None,
    level: str | None = None,
    card: str | None = None,
) -> enforcement.Outcome:
    """
    AI Chat: chat prompt → reply → enforcement. No entry summary and no closure
    rules; only the containment failsafe ends a chat (FR-AIR-013). Raises LLMError.
    """
    domains = domains or entry.domains or ["general"]
    skill = crisis.SKILL_FOR_HARM.get(safety.harm_type) if safety else None
    if skill:
        # The safety check chooses the skill, so a topic miss can't skip it.
        domains = [skill] + [d for d in domains if d != skill]
    entry.domains = domains
    # Once she said she is safe, or Echo asked, the question is not asked again
    # (unless danger is now: tier 1 asks directly).
    settled = enforcement.safety_settled(history, text)
    # Content-free (FR-CRIS-013): which skill, triage and card this reply used.
    logger.info("chat reply entry=%s skill=%s triage=%s card=%s safety_settled=%s safety=%s",
                entry.id, domains[0], level, card, settled, safety.model_dump() if safety else None)
    user_texts = [c for role, c in history if role == "user"] + [text]
    limit = capture.conversation_limits(context_builder.CHAT)["containment_turns"]
    closing = sum(1 for role, _ in history if role == "ai") >= (limit or settings.CONVERSATION_CONTAINMENT_TURNS)
    system_prompt = context_builder.build_chat_prompt(
        account,
        _focus_codes(db, account),
        domains,
        reply_language=language.reply_language(user_texts),
        closing=closing,
        level=level,
        safety=safety,
        card=card,
        safety_settled=settled,
    )
    # Examples are illustrations: a reply must not reuse their sentences, nor its own earlier ones.
    seen = ([c for role, c in history if role == "ai"]
            + md_loader.example_replies("journal_types", context_builder.CHAT)
            + md_loader.example_replies("skills", domains[0]))
    reply = llm_client.generate_chat_reply(system_prompt, history, text)
    copied = enforcement.repeated_sentences(reply.response_text, seen)
    if copied:
        # A reply that echoes an example reads like a script: one fresh try.
        rewrite = ("\n\n# Rewrite\nYour draft reused these sentences word for word. Say it in "
                   "new words:\n" + "\n".join(f"- {s}" for s in copied))
        reply = llm_client.generate_chat_reply(system_prompt + rewrite, history, text)
    return enforcement.enforce_chat(reply, closing=closing, seen=seen,
                                    no_safety_question=settled and level != crisis.TIER1)


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
        conversation_status=entry.conversation_status,
        preview=(preview[:120] + "…") if preview and len(preview) > 120 else preview,
    )


# ---------- endpoints ----------

@router.get("", response_model=list[EntrySummary])
def list_entries(
    status_: str = Query("all", alias="status", pattern="^(all|completed|in_progress)$"),
    since_days: int | None = Query(None, ge=1, le=3650),
    q: str | None = Query(None, min_length=1, max_length=100),
    journal_type: str | None = Query(None, max_length=50),
    limit: int = Query(50, ge=1, le=200),
    db: Session = Depends(get_db),
    account: Account = Depends(get_current_user),
):
    """
    FR-ENT-006: the user's entries, most recently touched first. Drafts are
    included by default so nothing she wrote is hidden; status=in_progress
    &since_days=7 gives the drafts the dashboard offers to resume (FR-HOME-002);
    journal_type=chat gives the AI Chat tab its chats.
    q searches what she wrote: her answers and every message of the thread.
    """
    query = db.query(Entry).filter(Entry.account_id == account.id)
    if journal_type:
        query = query.filter(Entry.journal_type == journal_type)
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
    if journal_type == context_builder.CHAT:
        # AI Chat: nothing to capture and no offer. She talks, Echo answers.
        entry = Entry(account_id=account.id, journal_type=journal_type, status="in_progress", conversation_status="active")
        db.add(entry)
        db.commit()
        db.refresh(entry)
        return _state(db, entry, account)
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


# ---------- daily check-in ----------
# The app owns the steps (mood → what's behind it → note) and sends everything
# once. Only the closing reply is written by the model. An empty step is just
# empty: no row, no message, nothing said about it.

CHECK_IN = "check_in"
LOW_STREAK = 3  # this many low check-ins in a row → a soft pointer to Chat


@router.get("/check_in/factors")
def check_in_factors(
    mood: int = Query(ge=1, le=5),
    db: Session = Depends(get_db),
    account: Account = Depends(get_current_user),
):
    """The chips for this mood, her own words first, and the step's fixed wording."""
    terms = db.query(UserTerm).filter(
        UserTerm.account_id == account.id, UserTerm.library == "triggers"
    ).order_by(UserTerm.created_at).all()
    return capture.checkin_factors(mood, [{"id": t.item_id, "name": t.name} for t in terms])


def _check_in_recorded(mood: int, factor_names: list[str], note: str) -> str:
    """What the model reads: plain labels, and only what she actually gave."""
    label = next(o["label"] for o in capture.SCALES["mood_5"] if o["value"] == mood)
    lines = [f"- Mood: {mood} ({label}) on a 1-5 scale"]
    if factor_names:
        lines.append(f"- What was part of it: {', '.join(factor_names)}")
    if note:
        lines.append(f"- Her note: <user_text>{note}</user_text>")
    return "\n".join(lines)


def _recent_closings(db: Session, account: Account, limit: int = 3) -> list[str]:
    rows = db.query(Message.content).join(Entry, Entry.id == Message.entry_id).filter(
        Entry.account_id == account.id, Entry.journal_type == CHECK_IN, Message.kind == "closing"
    ).order_by(Message.created_at.desc()).limit(limit).all()
    return [r.content for r in rows]


def _low_streak(db: Session, account: Account) -> bool:
    ids = [e.id for e in db.query(Entry.id).filter(
        Entry.account_id == account.id,
        Entry.journal_type.in_((CHECK_IN, "checkin")),
        Entry.status == "completed",
    ).order_by(Entry.completed_at.desc()).limit(LOW_STREAK).all()]
    if len(ids) < LOW_STREAK:
        return False
    moods = [json.loads(v.value) for v in db.query(CapturedValue).filter(
        CapturedValue.entry_id.in_(ids), CapturedValue.key == "mood"
    ).all()]
    return len(moods) == LOW_STREAK and all(m <= 2 for m in moods)


def _check_in_out(db: Session, entry: Entry, account: Account, event: crisis.CrisisResult | None = None) -> CheckInOut:
    closing = db.query(Message).filter(
        Message.entry_id == entry.id, Message.kind == "closing"
    ).order_by(Message.sequence.desc()).first()
    return CheckInOut(
        entry_id=entry.id,
        closing=closing.content if closing else "",
        card=closing.card if closing else None,
        crisis_event=event.as_dict() if event else None,
        suggest="chat" if _low_streak(db, account) else None,
    )


@router.post("/check_in", response_model=CheckInOut, status_code=status.HTTP_201_CREATED)
def submit_check_in(
    payload: CheckInCreate,
    db: Session = Depends(get_db),
    account: Account = Depends(get_current_user),
):
    existing = db.query(Entry).filter(Entry.client_id == payload.client_id).first()
    if existing:
        if existing.account_id != account.id:
            raise HTTPException(status_code=409, detail="Please try again")
        # A retry of one already saved: same entry, same reply.
        return _check_in_out(db, existing, account)

    names = capture._names("triggers", _user_terms(db, account))
    factors = list(dict.fromkeys(payload.factors))
    unknown = [f for f in factors if f not in names]
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown options: {unknown}")
    note = payload.note.strip()
    factor_names = [names[f] for f in factors]

    # Saved before any screening or model call, so nothing she gave can be lost.
    now = datetime.utcnow()
    entry = Entry(account_id=account.id, journal_type=CHECK_IN, status="completed", started_at=now,
                  completed_at=now, conversation_status="closed", closure_reason="journal_complete",
                  client_id=payload.client_id)
    db.add(entry)
    db.flush()
    label = next(o["label"] for o in capture.SCALES["mood_5"] if o["value"] == payload.mood)
    for key, value, shown in (("mood", payload.mood, f"{payload.mood} — {label}"),
                              ("triggers", factors, ", ".join(factor_names)),
                              ("trigger_note", note, note)):
        if value:
            db.add(CapturedValue(entry_id=entry.id, key=key, value=json.dumps(value, ensure_ascii=False), skipped=False))
            _add_message(db, entry, "user", "capture_answer", shown, value_id=key)
    db.commit()

    # The note is screened like every journal's free text. Danger or emergency:
    # fixed content, the card, and no model writes anything (FR-CRIS-006/007).
    event = card = None
    lang = language.reply_language([note] if note else [])
    if note:
        classification = llm_client.classify([], note, account.work_issues)
        tier, card = crisis.journal_tier(note, classification)
        event = crisis.record(db, account.id, entry, "trigger_note", tier, lang)
    if event is not None and event.text:
        _add_message(db, entry, "ai", "closing", event.text, card="prominent")
        logger.info("check-in reply entry=%s step=closing skill=none source=crisis_content tier=%s", entry.id, event.tier)
        db.commit()
        db.refresh(entry)
        return _check_in_out(db, entry, account, event)

    level = safety = None
    recent = _recent_closings(db, account)
    prompt = context_builder.build_checkin_closing_prompt(
        _focus_codes(db, account), _check_in_recorded(payload.mood, factor_names, note),
        lang, recent, level=level, safety=safety, card=card,
    )
    text = llm_client.generate_closing(prompt)
    if text:
        text = enforcement.limit_questions(enforcement.strip_card_labels(text), allowed=1)
        seen = recent + md_loader.example_replies("skills", CHECK_IN)
        if not text or text in recent or enforcement.repeated_sentences(text, seen):
            text = None
    source = "model" if text else "fallback"
    text = text or capture.checkin_fallback_closing(payload.mood, recent)
    _add_message(db, entry, "ai", "closing", text, card=card)
    # Content-free (FR-CRIS-013): which step and skill wrote the reply, and the safety outcome.
    logger.info("check-in reply entry=%s step=closing skill=check_in source=%s triage=%s card=%s",
                entry.id, source, level, card)
    db.commit()
    db.refresh(entry)
    return _check_in_out(db, entry, account, event)


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
    values = _values(db, entry, account)

    # A retry of an answer already recorded (the response was lost): the same
    # state back, nothing saved twice, no error for her to read.
    repeatable = {v["id"] for v in values if v.get("repeatable")}
    already = payload.value_id in _recorded(db, entry) and payload.value_id not in repeatable
    seen = payload.client_id is not None and db.query(Message.id).filter(
        Message.entry_id == entry.id, Message.client_id == payload.client_id
    ).first() is not None
    if already or seen:
        return _state(db, entry, account)

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
    spec = open_value or capture.next_value(values, _recorded(db, entry))
    if spec is None or spec["id"] != payload.value_id:
        raise HTTPException(status_code=409, detail=f"Expected value: {spec['id'] if spec else 'none'}")

    extra = _user_terms(db, account)
    row = _value_row(db, entry, spec["id"]) if spec.get("repeatable") else None
    finishing = bool(spec.get("repeatable")) and not payload.more

    # FR-ENT-003 / FR-ENT-005: persist the answer before crisis detection or any AI call.
    # An empty answer is stored as skipped (null) and gets no message: nothing
    # in the thread, and nothing the model reads, says she left it empty.
    if row is not None and finishing and not payload.value:
        # "Done writing" on what she has already sent: nothing new to record.
        value, shown = json.loads(row.value), None
    else:
        try:
            value = capture.validate(spec, payload.value, payload.skipped, extra)
        except capture.InvalidAnswer as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        shown = capture.display_text(spec, value, extra)
        if row is not None and value is not None:
            # Same value, another message: keep them together as one account.
            row.value = json.dumps(f"{json.loads(row.value)}\n\n{value}", ensure_ascii=False)
        elif row is None:
            db.add(CapturedValue(
                entry_id=entry.id,
                key=spec["id"],
                value=None if value is None else json.dumps(value, ensure_ascii=False),
                skipped=value is None,
                status="skipped" if value is None else "answered",
            ))
        if shown is not None:
            _add_message(db, entry, "user", "capture_answer", shown, value_id=spec["id"], client_id=payload.client_id)
    db.commit()

    event = card = None
    if shown is not None and spec["control"] == "free_text":
        event, _, card = _screen_journal(db, account, entry, spec["id"], shown, history=[])

    if spec.get("repeatable") and payload.more:
        # She is still writing: no new prompt, the same value stays open.
        db.commit()
        db.refresh(entry)
        return _state(db, entry, account, crisis_event=event)

    previous = None if value is None else f"{spec['id']} = {shown or 'written'}"
    _ask_next(db, entry, account, previous_answer=previous, card=card)
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
    is_chat = _journal_type(entry) == context_builder.CHAT
    if is_chat and entry.status == "in_progress":
        # A chat is a saved conversation from its first message (FR-ENT-020's counterpart).
        entry.status = "completed"
        entry.completed_at = datetime.utcnow()
    db.commit()

    lang = language.reply_language([c for role, c in history if role == "user"] + [text])
    journal_card = None
    if is_chat:
        event, classification = _screen(db, account, entry, "chat", text, history, lang)
    else:
        event, classification, journal_card = _screen_journal(db, account, entry, "chat", text, history, lang)
    if crisis.suppressed(entry):
        # FR-CRIS-006/007: no further generation for this entry (journals; see crisis.suppressed).
        _suppress_conversation(db, entry, account)
        db.commit()
        db.refresh(entry)
        return _state(db, entry, account, crisis_event=event, referral=True)

    # Always-on safety check, before the skill is chosen: a topic miss can't skip it.
    safety = classification.safety
    level = crisis.triage(event.tier if event else "clear", safety)
    if is_chat and level == crisis.TIER1 and entry.crisis_tier not in crisis.SUPPRESSING_TIERS:
        event = crisis.record(db, account.id, entry, "triage", "danger", lang)  # content-free record
    card = _card_due(db, entry, safety, level) if is_chat else journal_card

    if payload.more:
        # FR-JRN-003 in the conversation: she is still writing. The message is
        # stored and screened; Echo waits and answers all of it at once, because
        # interrupting someone mid-thought is the opposite of listening.
        db.commit()
        db.refresh(entry)
        return _state(db, entry, account, crisis_event=event)

    try:
        if is_chat:
            outcome = _chat_reply(db, entry, account, history, text, classification.domains, safety, level, card)
        else:
            outcome = _reflect(db, entry, account, history, text, classification.domains)
    except llm_client.LLMError:
        # A blocked or failed reply to a disclosure must not read like a shrug.
        disclosed = safety is not None and safety.harm_type in crisis.SKILL_FOR_HARM
        _add_message(db, entry, "ai", "notice",
                     crisis.pick(crisis.CONTENT["support_unavailable"], lang) if disclosed else REPLY_UNAVAILABLE,
                     card=card)
        db.commit()
        db.refresh(entry)
        return _state(db, entry, account, crisis_event=event)

    if outcome.crisis and not is_chat:
        # The model noticed risk the rules missed: its reply is replaced by fixed content.
        event = crisis.record(db, account.id, entry, "model", "danger", lang)
        _add_message(db, entry, "ai", "crisis", event.text)
        _suppress_conversation(db, entry, account)
    else:
        if is_chat and outcome.crisis:
            # AI Chat: the reply stays, and the card under it turns prominent.
            if entry.crisis_tier not in crisis.SUPPRESSING_TIERS:
                event = crisis.record(db, account.id, entry, "model", "danger", lang)
            card = _card_due(db, entry, safety, crisis.TIER1) or card
        # The card rides on the reply in its own field: drawn under it, never part of its text.
        _add_message(db, entry, "ai", "chat", outcome.reply_text, card=card)
        _apply_close(db, entry, account, outcome)
    db.commit()
    db.refresh(entry)
    return _state(db, entry, account, crisis_event=event, referral=outcome.referral)
