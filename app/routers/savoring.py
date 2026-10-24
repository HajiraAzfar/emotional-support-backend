"""
The Saving Journal's endpoints (core/savoring.py, core/savoring_report.py).

A moment is saved in one request at the end; the rules (three a day, today or
yesterday) come back as a 409 whose detail is {code, message}, worded for her.
Deleting a moment is the shared DELETE /entries/{id}.
"""
import uuid
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.core import savoring as sv, savoring_report, slots
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.account import Account
from app.models.entry import Entry
from app.models.user_term import UserTerm
from app.schemas.savoring import ActualIn, FavoriteIn, SavoringContent, SavoringCreate

router = APIRouter(tags=["savoring"])

TZ = Query(0, ge=-840, le=840)
# The longest stretch a heatmap or report covers in one request: a year and a week.
MAX_SPAN = timedelta(days=371)


def _terms(db: Session, account: Account, library: str | None = None) -> dict[str, str]:
    query = db.query(UserTerm).filter(UserTerm.account_id == account.id)
    if library:
        query = query.filter(UserTerm.library == library)
    return {t.item_id: t.name for t in query.all()}


def _today(tz_offset: int) -> date:
    return slots.local_now(tz_offset).date()


def _moment(db: Session, account: Account, entry_id: uuid.UUID) -> Entry:
    entry = db.query(Entry).filter(Entry.id == entry_id, Entry.account_id == account.id,
                                   Entry.journal_type == sv.JOURNAL).first()
    if entry is None:
        raise HTTPException(status_code=404, detail="Moment not found")
    return entry


def _one(db: Session, account: Account, entry: Entry, tz_offset: int) -> dict:
    recorded = sv.recorded_values(db, [entry]).get(entry.id, {})
    return sv.describe(entry, recorded, _terms(db, account), tz_offset, _today(tz_offset))


def _refused(exc: sv.Refused) -> HTTPException:
    return HTTPException(status_code=409, detail={"code": exc.code, "message": exc.message})


def _crisis(result) -> dict | None:
    if result is None:
        return None
    return {"tier": result.tier, "variant": result.variant, "text": result.text,
            "show_resources": result.tier in ("danger", "emergency")}


def _span(start: date | None, end: date | None, today: date, default_days: int) -> tuple[date, date]:
    end = end or today
    start = start or end - timedelta(days=default_days - 1)
    if start > end or end - start > MAX_SPAN:
        raise HTTPException(status_code=422, detail="from must be before to, and at most a year apart")
    return start, end


def _day(db: Session, account: Account, day: date, today: date) -> dict:
    count = sv.count_on(db, account.id, day)
    return {"date": day.isoformat(), "count": count, "cap": sv.CAP, "at_cap": count >= sv.CAP,
            "message": sv.cap_message(day, today) if count >= sv.CAP else None}


@router.get("/savoring/home")
def savoring_home(tz_offset: int = TZ, db: Session = Depends(get_db), account: Account = Depends(get_current_user)):
    """Everything the journal's home shows but the heatmap: today's room, the last three, follow-ups, memory lane."""
    today = _today(tz_offset)
    rows = sv.described(db, account.id, _terms(db, account), tz_offset, today)
    return {
        "today": _day(db, account, today, today),
        "yesterday": _day(db, account, today - timedelta(days=1), today),
        "total_entries": len(rows),
        "recent": rows[:3],
        "follow_ups": sv.follow_ups(rows, today),
        "memory_lane": sv.memory_lane(rows, today, account.id),
        "support": sv.support(db, account.id, rows),
    }


@router.get("/savoring/prefill")
def savoring_prefill(source_entry_id: uuid.UUID, db: Session = Depends(get_db),
                     account: Account = Depends(get_current_user)):
    """The positive feelings of the check-in a moment is opened from, to start ticked."""
    try:
        return {"feelings": sv.positive_feelings_of(db, account.id, source_entry_id)}
    except sv.Refused as exc:
        raise _refused(exc)


@router.post("/savoring/entries", status_code=201)
def create_moment(payload: SavoringCreate, db: Session = Depends(get_db),
                  account: Account = Depends(get_current_user)):
    try:
        entry, result = sv.create(db, account, payload, _terms(db, account, sv.FEELINGS))
    except sv.Refused as exc:
        raise _refused(exc)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    today = _today(payload.tz_offset)
    return {"entry": _one(db, account, entry, payload.tz_offset), "crisis_event": _crisis(result),
            "day": _day(db, account, entry.local_date, today)}


@router.get("/savoring/entries")
def list_moments(
    type_: str | None = Query(None, alias="type", pattern="^(moment|reminisce|anticipate)$"),
    emotion: str | None = Query(None, max_length=60),
    favorite: bool = False,
    day: date | None = Query(None, alias="date"),
    tz_offset: int = TZ,
    db: Session = Depends(get_db),
    account: Account = Depends(get_current_user),
):
    """Her moments, newest first, filtered by kind, a feeling, favourites or one day."""
    rows = sv.described(db, account.id, _terms(db, account), tz_offset, _today(tz_offset))
    if type_:
        rows = [r for r in rows if r["type"] == type_]
    if emotion:
        rows = [r for r in rows if any(e["id"] == emotion for e in r["emotions"])]
    if favorite:
        rows = [r for r in rows if r["is_favorite"]]
    if day:
        rows = [r for r in rows if r["local_date"] == day.isoformat()]
    return {"entries": rows}


@router.get("/savoring/entries/{entry_id}")
def get_moment(entry_id: uuid.UUID, tz_offset: int = TZ, db: Session = Depends(get_db),
               account: Account = Depends(get_current_user)):
    return _one(db, account, _moment(db, account, entry_id), tz_offset)


@router.put("/savoring/entries/{entry_id}")
def edit_moment(entry_id: uuid.UUID, payload: SavoringContent, tz_offset: int = TZ,
                db: Session = Depends(get_db), account: Account = Depends(get_current_user)):
    """Her edit replaces what the moment says; its day, how it was captured and its follow-up stay."""
    entry = _moment(db, account, entry_id)
    try:
        entry, result = sv.update(db, account, entry, payload, _terms(db, account, sv.FEELINGS))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"entry": _one(db, account, entry, tz_offset), "crisis_event": _crisis(result)}


@router.put("/savoring/entries/{entry_id}/favorite")
def favorite_moment(entry_id: uuid.UUID, payload: FavoriteIn, tz_offset: int = TZ,
                    db: Session = Depends(get_db), account: Account = Depends(get_current_user)):
    entry = _moment(db, account, entry_id)
    entry.is_favorite = payload.is_favorite
    db.commit()
    return _one(db, account, entry, tz_offset)


@router.post("/savoring/entries/{entry_id}/actual")
def moment_actual(entry_id: uuid.UUID, payload: ActualIn, tz_offset: int = TZ,
                  db: Session = Depends(get_db), account: Account = Depends(get_current_user)):
    """"How was it really?" for a moment she looked forward to, once its day has come."""
    entry = _moment(db, account, entry_id)
    try:
        sv.set_actual(db, entry, payload.actual_rating, _today(tz_offset))
    except sv.Refused as exc:
        raise _refused(exc)
    return _one(db, account, entry, tz_offset)


@router.get("/savoring/heatmap")
def savoring_heatmap(
    start: date | None = Query(None, alias="from"),
    end: date | None = Query(None, alias="to"),
    tz_offset: int = TZ,
    db: Session = Depends(get_db),
    account: Account = Depends(get_current_user),
):
    """[{date, points, level, grace_day}] on her days (12 weeks unless asked), with the streak beside it."""
    today = _today(tz_offset)
    start, end = _span(start, end, today, 12 * 7)
    rows = sv.described(db, account.id, {}, tz_offset, today)
    return sv.heatmap(rows, start, end, today)


@router.get("/reports/savoring")
def savoring_report_view(
    start: date | None = Query(None, alias="from"),
    end: date | None = Query(None, alias="to"),
    tz_offset: int = TZ,
    db: Session = Depends(get_db),
    account: Account = Depends(get_current_user),
):
    """The in-app report for a stretch of days (the last 30 unless asked)."""
    today = _today(tz_offset)
    start, end = _span(start, end, today, 30)
    rows = sv.described(db, account.id, _terms(db, account), tz_offset, today)
    moods = savoring_report.checkin_moods(db, account.id, start, end, tz_offset)
    return savoring_report.build(rows, start, end, moods)


@router.get("/savoring/export.csv")
def savoring_export(
    start: date | None = Query(None, alias="from"),
    end: date | None = Query(None, alias="to"),
    include_notes: bool = False,
    tz_offset: int = TZ,
    db: Session = Depends(get_db),
    account: Account = Depends(get_current_user),
):
    """Her moments as a CSV, one row each. Her own words only with include_notes (off unless she turns it on)."""
    today = _today(tz_offset)
    start, end = _span(start, end, today, 366)
    rows = savoring_report.in_period(sv.described(db, account.id, _terms(db, account), tz_offset, today),
                                     start, end)
    return Response(
        content=savoring_report.to_csv(rows, include_notes),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="mind-doc-savoring-{today.isoformat()}.csv"',
                 "Cache-Control": "no-store"},
    )
