"""
The Exposure Journal's endpoints (core/exposure.py, core/exposure_report.py).

Ladders are edited here (online); exposures are saved by the app's client id
at every step, from the first draft to how it went. Deleting an exposure is
the shared DELETE /entries/{id}.
"""
import uuid
from datetime import date, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.core import exposure as ex, exposure_report, slots
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.account import Account
from app.models.entry import Entry
from app.models.focus_area import FocusArea
from app.models.user_term import UserTerm
from app.schemas.exposure import ExposureSave, FavoriteIn, LadderCreate, LadderUpdate, Reorder, StepIn, StepUpdate

router = APIRouter(tags=["exposure"])

TZ = Query(0, ge=-840, le=840)
MAX_SPAN = timedelta(days=371)


def _terms(db: Session, account: Account) -> dict[str, str]:
    return {t.item_id: t.name for t in db.query(UserTerm).filter(UserTerm.account_id == account.id).all()}


def _today(tz_offset: int) -> date:
    return slots.local_now(tz_offset).date()


def _refused(exc: ex.Refused) -> HTTPException:
    return HTTPException(status_code=404 if exc.code == "not_found" else 409,
                         detail={"code": exc.code, "message": exc.message})


def _crisis(result) -> dict | None:
    if result is None:
        return None
    return {"tier": result.tier, "variant": result.variant, "text": result.text,
            "show_resources": result.tier in ("danger", "emergency")}


def _record(db: Session, account: Account, entry_id: uuid.UUID) -> Entry:
    entry = db.query(Entry).filter(Entry.id == entry_id, Entry.account_id == account.id,
                                   Entry.journal_type == ex.JOURNAL).first()
    if entry is None:
        raise HTTPException(status_code=404, detail="Record not found")
    return entry


def _one(db: Session, account: Account, entry: Entry, tz_offset: int) -> dict:
    rec = ex.records_of(db, [entry]).get(entry.id, {})
    return ex.describe(entry, rec, _terms(db, account), tz_offset, ex.step_lookup(db, account.id))


def _span(start: date | None, end: date | None, today: date, default_days: int) -> tuple[date, date]:
    end = end or today
    start = start or end - timedelta(days=default_days - 1)
    if start > end or end - start > MAX_SPAN:
        raise HTTPException(status_code=422, detail="from must be before to, and at most a year apart")
    return start, end


def _ladders_now(db: Session, account: Account, tz_offset: int, include_archived: bool = False) -> list[dict]:
    return ex.ladders(db, account.id, ex.described(db, account.id, {}, tz_offset), include_archived)


# ---------- ladders ----------

@router.get("/exposure/ladders")
def list_ladders(archived: bool = False, tz_offset: int = TZ, db: Session = Depends(get_db),
                 account: Account = Depends(get_current_user)):
    """Her ladders, steps in order, each with how it has gone so far."""
    return {"ladders": _ladders_now(db, account, tz_offset, archived)}


@router.post("/exposure/ladders", status_code=201)
def create_ladder(payload: LadderCreate, db: Session = Depends(get_db), account: Account = Depends(get_current_user)):
    try:
        ladder, result = ex.create_ladder(db, account, payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"ladder_id": str(ladder.id), "ladders": _ladders_now(db, account, 0), "crisis_event": _crisis(result)}


@router.put("/exposure/ladders/{ladder_id}")
def update_ladder(ladder_id: uuid.UUID, payload: LadderUpdate, db: Session = Depends(get_db),
                  account: Account = Depends(get_current_user)):
    try:
        _, result = ex.update_ladder(db, account, ladder_id, payload)
    except ex.Refused as exc:
        raise _refused(exc)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return {"ladders": _ladders_now(db, account, 0), "crisis_event": _crisis(result)}


@router.delete("/exposure/ladders/{ladder_id}")
def remove_ladder(ladder_id: uuid.UUID, db: Session = Depends(get_db), account: Account = Depends(get_current_user)):
    """Archived when one of its steps was used (records keep their step), deleted otherwise."""
    try:
        return {"outcome": ex.remove_ladder(db, account, ladder_id), "ladders": _ladders_now(db, account, 0)}
    except ex.Refused as exc:
        raise _refused(exc)


@router.post("/exposure/ladders/{ladder_id}/steps", status_code=201)
def add_step(ladder_id: uuid.UUID, payload: StepIn, db: Session = Depends(get_db),
             account: Account = Depends(get_current_user)):
    try:
        _, result = ex.add_step(db, account, ladder_id, payload)
    except ex.Refused as exc:
        raise _refused(exc)
    return {"ladders": _ladders_now(db, account, 0), "crisis_event": _crisis(result)}


@router.put("/exposure/ladders/{ladder_id}/steps/{item_id}")
def update_step(ladder_id: uuid.UUID, item_id: uuid.UUID, payload: StepUpdate, db: Session = Depends(get_db),
                account: Account = Depends(get_current_user)):
    try:
        _, result = ex.update_step(db, account, ladder_id, item_id, payload)
    except ex.Refused as exc:
        raise _refused(exc)
    return {"ladders": _ladders_now(db, account, 0), "crisis_event": _crisis(result)}


@router.delete("/exposure/ladders/{ladder_id}/steps/{item_id}")
def remove_step(ladder_id: uuid.UUID, item_id: uuid.UUID, db: Session = Depends(get_db),
                account: Account = Depends(get_current_user)):
    """A step an exposure points at is archived (its name stays in the records); any other is deleted."""
    try:
        return {"outcome": ex.remove_step(db, account, ladder_id, item_id), "ladders": _ladders_now(db, account, 0)}
    except ex.Refused as exc:
        raise _refused(exc)


@router.put("/exposure/ladders/{ladder_id}/order")
def reorder_steps(ladder_id: uuid.UUID, payload: Reorder, db: Session = Depends(get_db),
                  account: Account = Depends(get_current_user)):
    try:
        ex.reorder(db, account, ladder_id, payload.item_ids)
    except ex.Refused as exc:
        raise _refused(exc)
    return {"ladders": _ladders_now(db, account, 0)}


# ---------- exposures ----------

@router.get("/exposure/home")
def exposure_home(tz_offset: int = TZ, db: Session = Depends(get_db), account: Account = Depends(get_current_user)):
    """Drafts and plans (a week at most), the last three, today's count, the gentle signals, the screening hint."""
    ex.expire_drafts(db, account.id)
    today = _today(tz_offset)
    rows = ex.described(db, account.id, _terms(db, account), tz_offset)
    focus = {f.code for f in db.query(FocusArea).filter(FocusArea.account_id == account.id).all()}
    finished = [r for r in rows if r["status"] in ex.FINISHED]
    return {
        "drafts": [r for r in rows if r["status"] == ex.DRAFT],
        "planned": [r for r in rows if r["status"] == ex.PLANNED],
        "recent": finished[:3],
        "total_done": sum(1 for r in finished if r["status"] in ex.COUNTED),
        "today_done": ex.finished_on(rows, today),
        "gentle": ex.finished_on(rows, today) >= ex.GENTLE_AFTER,
        "pattern": ex.no_change(rows),
        # Trauma-related distress, or a very high baseline: suggest a professional (never a block).
        "suggest_professional": "trauma_ptsd" in focus or (account.distress_baseline or 0) >= ex.HIGH_BASELINE,
        "ladders": ex.ladders(db, account.id, rows),
    }


@router.get("/exposure/prefill")
def exposure_prefill(source_entry_id: uuid.UUID, db: Session = Depends(get_db),
                     account: Account = Depends(get_current_user)):
    """The feelings of the check-in or thought record an exposure is planned from."""
    try:
        return {"feelings": ex.source_feelings(db, account.id, source_entry_id)}
    except ex.Refused as exc:
        raise _refused(exc)


@router.put("/exposure/records/{client_id}")
def save_record(client_id: uuid.UUID, payload: ExposureSave, db: Session = Depends(get_db),
                account: Account = Depends(get_current_user)):
    """A draft step, the plan (planned), how it went (completed / partial / skipped), or a correction."""
    try:
        entry, result = ex.save(db, account, client_id, payload, _terms(db, account))
    except ex.Refused as exc:
        raise _refused(exc)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    rows = ex.described(db, account.id, {}, payload.tz_offset)
    record = _one(db, account, entry, payload.tz_offset)
    counted = record["status"] in ex.COUNTED
    return {
        "record": record,
        "crisis_event": _crisis(result),
        "gentle": counted and ex.finished_on(rows, ex.day_of(entry, payload.tz_offset)) >= ex.GENTLE_AFTER,
        "pattern": ex.no_change(rows) if counted else None,
    }


@router.get("/exposure/entries")
def list_records(tz_offset: int = TZ, day: date | None = Query(None, alias="date"), favorite: bool = False,
                 db: Session = Depends(get_db), account: Account = Depends(get_current_user)):
    """Finished exposures (done, in part, or set aside), newest first, those from before too."""
    rows = ex.described(db, account.id, _terms(db, account), tz_offset, statuses=ex.FINISHED)
    if favorite:
        rows = [r for r in rows if r["is_favorite"]]
    if day:
        rows = [r for r in rows if r["local_date"] == day.isoformat()]
    return {"entries": rows}


@router.get("/exposure/entries/{entry_id}")
def get_record(entry_id: uuid.UUID, tz_offset: int = TZ, db: Session = Depends(get_db),
               account: Account = Depends(get_current_user)):
    """One exposure, a plan or finished; one from before gets a client id, so it can be carried on."""
    entry = _record(db, account, entry_id)
    ex.client_id_of(db, entry)
    return _one(db, account, entry, tz_offset)


@router.put("/exposure/entries/{entry_id}/favorite")
def favorite_record(entry_id: uuid.UUID, payload: FavoriteIn, tz_offset: int = TZ, db: Session = Depends(get_db),
                    account: Account = Depends(get_current_user)):
    entry = _record(db, account, entry_id)
    entry.is_favorite = payload.is_favorite
    db.commit()
    return _one(db, account, entry, tz_offset)


@router.get("/exposure/heatmap")
def exposure_heatmap(start: date | None = Query(None, alias="from"), end: date | None = Query(None, alias="to"),
                     tz_offset: int = TZ, db: Session = Depends(get_db), account: Account = Depends(get_current_user)):
    """Days with an exposure done fully or in part (12 weeks unless asked), with the streak beside it."""
    today = _today(tz_offset)
    start, end = _span(start, end, today, 12 * 7)
    return ex.heatmap(ex.described(db, account.id, {}, tz_offset), start, end, today)


@router.get("/reports/exposure")
def exposure_report_view(start: date | None = Query(None, alias="from"), end: date | None = Query(None, alias="to"),
                         tz_offset: int = TZ, db: Session = Depends(get_db),
                         account: Account = Depends(get_current_user)):
    """The in-app report for a stretch of days (the last 30 unless asked)."""
    today = _today(tz_offset)
    start, end = _span(start, end, today, 30)
    rows = ex.described(db, account.id, _terms(db, account), tz_offset)
    return exposure_report.build(rows, ex.ladders(db, account.id, rows, include_archived=True), start, end)


@router.get("/exposure/export.json")
def exposure_export(include_notes: bool = False, tz_offset: int = TZ, db: Session = Depends(get_db),
                    account: Account = Depends(get_current_user)):
    """Her ladders, steps and exposures; her own notes only with include_notes. Nothing caches it."""
    from fastapi.responses import JSONResponse

    rows = ex.described(db, account.id, _terms(db, account), tz_offset)
    body = exposure_report.export(rows, ex.ladders(db, account.id, rows, include_archived=True), include_notes)
    return JSONResponse(body, headers={"Cache-Control": "no-store",
                                       "Content-Disposition": 'attachment; filename="mind-doc-exposure.json"'})
