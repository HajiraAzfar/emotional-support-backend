import uuid
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.models.account import Account
from app.models.entry import Entry
from app.models.message import Message
from app.schemas.entry import EntryCreate, EntryOut
from app.schemas.message import MessageCreate, MessageOut

router = APIRouter(prefix="/entries", tags=["entries"])


def _attach_messages(db: Session, entry: Entry) -> Entry:
    messages = db.query(Message).filter(
        Message.entry_id == entry.id
    ).order_by(Message.sequence).all()
    entry.messages = messages
    return entry


@router.post("", response_model=EntryOut, status_code=status.HTTP_201_CREATED)
def create_entry(
    payload: EntryCreate,
    db: Session = Depends(get_db),
    account: Account = Depends(get_current_user),
):
    entry = Entry(
        account_id=account.id,
        journal_type=payload.journal_type,
        status="in_progress",
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return _attach_messages(db, entry)


@router.get("/{entry_id}", response_model=EntryOut)
def get_entry(
    entry_id: uuid.UUID,
    db: Session = Depends(get_db),
    account: Account = Depends(get_current_user),
):
    entry = db.query(Entry).filter(
        Entry.id == entry_id,
        Entry.account_id == account.id,
    ).first()
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")
    return _attach_messages(db, entry)


@router.post("/{entry_id}/messages", response_model=MessageOut, status_code=status.HTTP_201_CREATED)
def add_message(
    entry_id: uuid.UUID,
    payload: MessageCreate,
    db: Session = Depends(get_db),
    account: Account = Depends(get_current_user),
):
    entry = db.query(Entry).filter(
        Entry.id == entry_id,
        Entry.account_id == account.id,
    ).first()
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")

    last_seq = db.query(Message).filter(Message.entry_id == entry_id).count()
    message = Message(
        entry_id=entry_id,
        role="user",
        content=payload.content,
        sequence=last_seq + 1,
    )
    db.add(message)
    db.commit()
    db.refresh(message)
    return message


@router.put("/{entry_id}/complete", response_model=EntryOut)
def complete_entry(
    entry_id: uuid.UUID,
    db: Session = Depends(get_db),
    account: Account = Depends(get_current_user),
):
    entry = db.query(Entry).filter(
        Entry.id == entry_id,
        Entry.account_id == account.id,
    ).first()
    if not entry:
        raise HTTPException(status_code=404, detail="Entry not found")

    entry.status = "completed"
    entry.completed_at = datetime.utcnow()
    db.commit()
    db.refresh(entry)
    return _attach_messages(db, entry)