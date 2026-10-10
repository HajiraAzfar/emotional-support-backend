"""
Selection libraries (FR-PICK-001 to 003): thinking traps, feelings, triggers.
Each user sees her own terms first and categories linked to her focus areas
next (FR-PICK-005, FR-PICK-006).
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core import capture, crisis, llm_client
from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.account import Account
from app.models.entry import Entry
from app.models.focus_area import FocusArea
from app.models.message import Message
from app.models.user_term import UserTerm
from app.schemas.entry import CrisisEventOut

router = APIRouter(prefix="/libraries", tags=["libraries"])


class TermCreate(BaseModel):
    name: str = Field(min_length=1, max_length=capture.USER_TERM_MAX_LENGTH)
    # The entry the term was added from, so a danger tier suppresses AI for that entry too.
    entry_id: uuid.UUID | None = None


class TermOut(BaseModel):
    id: str
    name: str


class TermCreated(BaseModel):
    term: TermOut
    crisis_event: CrisisEventOut | None = None


def _terms(db: Session, account: Account, library: str) -> list[UserTerm]:
    return db.query(UserTerm).filter(
        UserTerm.account_id == account.id, UserTerm.library == library
    ).order_by(UserTerm.created_at).all()


@router.get("/{name}")
def get_library(
    name: str,
    prefer: str | None = None,
    db: Session = Depends(get_db),
    account: Account = Depends(get_current_user),
):
    if name not in capture.LIBRARIES:
        raise HTTPException(status_code=404, detail="Library not found")
    focus_codes = [f.code for f in db.query(FocusArea).filter(FocusArea.account_id == account.id).all()]
    terms = [{"id": t.item_id, "name": t.name} for t in _terms(db, account, name)]
    return capture.library_for_user(name, focus_codes, terms, prefer_valence=prefer)


@router.post("/{name}/terms", response_model=TermCreated, status_code=status.HTTP_201_CREATED)
def add_term(
    name: str,
    payload: TermCreate,
    db: Session = Depends(get_db),
    account: Account = Depends(get_current_user),
):
    if name not in capture.EXTENDABLE_LIBRARIES:
        raise HTTPException(status_code=404, detail="This library does not accept new terms")
    term_name = " ".join(payload.name.split())
    if not term_name:
        raise HTTPException(status_code=422, detail="Term cannot be empty")

    existing = next((t for t in _terms(db, account, name) if t.name.lower() == term_name.lower()), None)
    if existing:
        return TermCreated(term=TermOut(id=existing.item_id, name=existing.name))

    entry = None
    if payload.entry_id:
        entry = db.query(Entry).filter(Entry.id == payload.entry_id, Entry.account_id == account.id).first()

    # FR-PICK-007: user-defined terms go through the same crisis detection as
    # journal text. Pickers exist only in journals, so the journal rules apply.
    classification = (llm_client.Classification(domains=None, risk_tier=None) if crisis.suppressed(entry)
                      else llm_client.classify([], term_name, None))
    tier, _ = crisis.journal_tier(term_name, classification)
    event = crisis.record(db, account.id, entry, f"term:{name}", tier)
    if entry is not None and event and event.text:
        sequence = db.query(Message).filter(Message.entry_id == entry.id).count() + 1
        db.add(Message(entry_id=entry.id, role="ai", kind="crisis", content=event.text, sequence=sequence))

    term = UserTerm(account_id=account.id, library=name, name=term_name)
    db.add(term)
    db.commit()
    db.refresh(term)
    return TermCreated(
        term=TermOut(id=term.item_id, name=term.name),
        crisis_event=event.as_dict() if event else None,
    )
