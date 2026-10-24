"""
The Thought Journal's endpoints (core/thought.py, core/thought_report.py).

A record is saved by the app's client id at every step (a draft) and once more
to finish it; the same call carries a later change. Deleting a record is the
shared DELETE /entries/{id}.
"""
import uuid
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core import slots, thought as th, thought_report
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.account import Account
from app.models.entry import Entry
from app.models.focus_area import FocusArea
from app.models.user_term import UserTerm
from app.schemas.thought import FavoriteIn, QuestionIn, ThoughtContent, ThoughtSave

router = APIRouter(tags=["thought"])

TZ = Query(0, ge=-840, le=840)
MAX_SPAN = timedelta(days=371)


def _terms(db: Session, account: Account) -> dict[str, str]:
    return {t.item_id: t.name for t in db.query(UserTerm).filter(UserTerm.account_id == account.id).all()}


def _focus(db: Session, account: Account) -> list[str]:
    return [f.code for f in db.query(FocusArea).filter(FocusArea.account_id == account.id).all()]


def _today(tz_offset: int) -> date:
    return slots.local_now(tz_offset).date()


def _record(db: Session, account: Account, entry_id: uuid.UUID) -> Entry:
    entry = db.query(Entry).filter(Entry.id == entry_id, Entry.account_id == account.id,
                                   Entry.journal_type == th.JOURNAL).first()
    if entry is None:
        raise HTTPException(status_code=404, detail="Record not found")
    return entry


def _one(db: Session, account: Account, entry: Entry, tz_offset: int) -> dict:
    recorded = th.recorded_values(db, [entry]).get(entry.id, {})
    return th.describe(entry, recorded, _terms(db, account), tz_offset)


def _crisis(result) -> dict | None:
    if result is None:
        return None
    return {"tier": result.tier, "variant": result.variant, "text": result.text,
            "show_resources": result.tier in ("danger", "emergency")}


def _after_save(db: Session, account: Account, entry: Entry, result, tz_offset: int) -> dict:
    """The record, and when it is finished: a kind word from the third of a day, and a recurring theme."""
    out = {"record": _one(db, account, entry, tz_offset), "crisis_event": _crisis(result), "gentle": False,
           "pattern": None}
    if entry.status == "completed":
        rows = th.described(db, account.id, _terms(db, account), tz_offset)
        out["gentle"] = th.completed_on(rows, th.day_of(entry, tz_offset)) >= th.GENTLE_AFTER
        out["pattern"] = th.recurring(rows, _today(tz_offset))
    return out


def _span(start: date | None, end: date | None, today: date, default_days: int) -> tuple[date, date]:
    end = end or today
    start = start or end - timedelta(days=default_days - 1)
    if start > end or end - start > MAX_SPAN:
        raise HTTPException(status_code=422, detail="from must be before to, and at most a year apart")
    return start, end


@router.get("/thought/home")
def thought_home(tz_offset: int = TZ, db: Session = Depends(get_db), account: Account = Depends(get_current_user)):
    """Drafts to carry on (a week at most), the last three, today's count, and the gentle signals."""
    th.expire_drafts(db, account.id)
    today = _today(tz_offset)
    terms, focus = _terms(db, account), _focus(db, account)
    rows = th.described(db, account.id, terms, tz_offset, status=None)
    done = [r for r in rows if r["status"] == "completed"]
    return {
        "drafts": [r for r in rows if r["status"] == "in_progress"],
        "recent": done[:3],
        "total_completed": len(done),
        "today_completed": th.completed_on(done, today),
        "gentle": th.completed_on(done, today) >= th.GENTLE_AFTER,
        "pattern": th.recurring(done, today),
        # The trauma variant (FR-JRN-005): no "what makes it feel true", and the situation optional.
        "asks_evidence_for": th.asks_supporting_evidence(focus),
        "situation_required": th.situation_required(focus),
    }


@router.get("/thought/prefill")
def thought_prefill(source_entry_id: uuid.UUID, db: Session = Depends(get_db),
                    account: Account = Depends(get_current_user)):
    """The feelings of the check-in a record is opened from, to start ticked (hers to change)."""
    try:
        return {"feelings": th.checkin_feelings(db, account.id, source_entry_id)}
    except th.Refused as exc:
        raise HTTPException(status_code=409, detail={"code": exc.code, "message": exc.message})


@router.put("/thought/records/{client_id}")
def save_record(client_id: uuid.UUID, payload: ThoughtSave, db: Session = Depends(get_db),
                account: Account = Depends(get_current_user)):
    """A draft step, the finished record (complete=true), or a later change: the same record each time."""
    try:
        entry, result = th.save(db, account, client_id, payload, _terms(db, account), _focus(db, account))
    except th.Refused as exc:
        raise HTTPException(status_code=409, detail={"code": exc.code, "message": exc.message})
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return _after_save(db, account, entry, result, payload.tz_offset)


@router.get("/thought/entries")
def list_records(
    tag: str | None = Query(None, max_length=60),
    trap: str | None = Query(None, max_length=60),
    favorite: bool = False,
    day: date | None = Query(None, alias="date"),
    tz_offset: int = TZ,
    db: Session = Depends(get_db),
    account: Account = Depends(get_current_user),
):
    """Finished records, newest first (those from before the module too)."""
    rows = th.described(db, account.id, _terms(db, account), tz_offset)
    if tag:
        rows = [r for r in rows if any(t["id"] == tag for t in r["situation_tags"])]
    if trap:
        rows = [r for r in rows if any(t["id"] == trap for t in r["thinking_traps"])]
    if favorite:
        rows = [r for r in rows if r["is_favorite"]]
    if day:
        rows = [r for r in rows if r["local_date"] == day.isoformat()]
    return {"entries": rows}


@router.get("/thought/entries/{entry_id}")
def get_record(entry_id: uuid.UUID, tz_offset: int = TZ, db: Session = Depends(get_db),
               account: Account = Depends(get_current_user)):
    """One record, finished or a draft. A record from before gets a client id, so it can be carried on."""
    entry = _record(db, account, entry_id)
    th.client_id_of(db, entry)
    return _one(db, account, entry, tz_offset)


@router.put("/thought/entries/{entry_id}")
def edit_record(entry_id: uuid.UUID, payload: ThoughtContent, tz_offset: int = TZ, db: Session = Depends(get_db),
                account: Account = Depends(get_current_user)):
    """A change to a finished record: free for a day, marked edited after that."""
    entry = _record(db, account, entry_id)
    if entry.status != "completed":
        raise HTTPException(status_code=409, detail="A draft is saved through its record")
    try:
        entry, result = th.edit(db, account, entry, payload, _terms(db, account), _focus(db, account))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return _after_save(db, account, entry, result, tz_offset)


@router.put("/thought/entries/{entry_id}/favorite")
def favorite_record(entry_id: uuid.UUID, payload: FavoriteIn, tz_offset: int = TZ, db: Session = Depends(get_db),
                    account: Account = Depends(get_current_user)):
    entry = _record(db, account, entry_id)
    entry.is_favorite = payload.is_favorite
    db.commit()
    return _one(db, account, entry, tz_offset)


@router.post("/thought/entries/{entry_id}/question")
def assist_question(entry_id: uuid.UUID, payload: QuestionIn, db: Session = Depends(get_db),
                    account: Account = Depends(get_current_user)):
    """"Help me think": one open question about the evidence, or null (the app then asks a fixed one)."""
    return {"question": th.question(db, _record(db, account, entry_id), payload.side)}


@router.post("/thought/entries/{entry_id}/traps")
def assist_traps(entry_id: uuid.UUID, db: Session = Depends(get_db), account: Account = Depends(get_current_user)):
    """Up to two thinking patterns that might fit, for her to accept or reject; null when unavailable."""
    return {"suggestions": th.suggest_traps(db, _record(db, account, entry_id))}


@router.get("/thought/heatmap")
def thought_heatmap(
    start: date | None = Query(None, alias="from"),
    end: date | None = Query(None, alias="to"),
    tz_offset: int = TZ,
    db: Session = Depends(get_db),
    account: Account = Depends(get_current_user),
):
    """Days with a finished record (12 weeks unless asked), with the streak beside it."""
    today = _today(tz_offset)
    start, end = _span(start, end, today, 12 * 7)
    return th.heatmap(th.described(db, account.id, {}, tz_offset), start, end, today)


@router.get("/reports/thought")
def thought_report_view(
    start: date | None = Query(None, alias="from"),
    end: date | None = Query(None, alias="to"),
    tz_offset: int = TZ,
    db: Session = Depends(get_db),
    account: Account = Depends(get_current_user),
):
    """The in-app report for a stretch of days (the last 30 unless asked)."""
    today = _today(tz_offset)
    start, end = _span(start, end, today, 30)
    rows = th.described(db, account.id, _terms(db, account), tz_offset, status=None)
    return thought_report.build(rows, start, end, thought_report.high_checkins(db, account.id, rows))
