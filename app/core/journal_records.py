"""
What the app-owned journals (Thought, Exposure) share: a record kept by the
app's own client id (so a retry or an offline sync is the same record), its
values replaced at every save, the words that changed screened at once, drafts
let go after a week, and the day a record counts on.
"""
import json
import uuid
from datetime import date, datetime, timedelta

from sqlalchemy.orm import Session

from app.core import crisis, insights, language, llm_client
from app.models.captured_value import CapturedValue
from app.models.entry import Entry


class Refused(Exception):
    """A save the rules do not allow; `code` and `message` go to the app as they are."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def entry_for(db: Session, account, client_id: uuid.UUID, journal: str) -> Entry | None:
    entry = db.query(Entry).filter(Entry.client_id == client_id).first()
    if entry is not None and (entry.account_id != account.id or entry.journal_type != journal):
        raise Refused("conflict", "That record could not be saved.")
    return entry


def store(db: Session, entry: Entry, values: dict, keys: tuple):
    """Replace the record's `keys` with `values` (what is not given is left out)."""
    db.query(CapturedValue).filter(
        CapturedValue.entry_id == entry.id, CapturedValue.key.in_(keys)
    ).delete(synchronize_session=False)
    for key, value in values.items():
        db.add(CapturedValue(entry_id=entry.id, key=key, value=json.dumps(value, ensure_ascii=False),
                             skipped=False, status="answered"))


def screen_changes(db: Session, account, entry: Entry, old: dict, new: dict, free_text: tuple):
    """Only words that changed in this save are read: each step, and the final save, are screened once."""
    from app.routers.entries import _screen_journal  # the shared journal screening

    changed = [k for k in free_text if new.get(k) and new.get(k) != old.get(k)]
    if not changed:
        return None
    result, _, _ = _screen_journal(db, account, entry, changed[0], "\n".join(new[k] for k in changed), history=[])
    return result


def screen_text(db: Session, account, field: str, text: str):
    """Words written outside any entry (a fear ladder): screened the same way; a flag has no entry."""
    if not text.strip():
        return None
    classification = llm_client.classify([], text, account.work_issues)
    tier, _ = crisis.journal_tier(text, classification)
    return crisis.record(db, account.id, None, field, tier, language.ENGLISH)


def expire_drafts(db: Session, account_id, journal: str, days: int, now_utc: datetime | None = None) -> int:
    """Drafts are kept `days`; older ones go (a safety flag raised in one stays, unlinked)."""
    from app.routers.entries import remove_entry

    cutoff = (now_utc or datetime.utcnow()) - timedelta(days=days)
    old = db.query(Entry).filter(Entry.account_id == account_id, Entry.journal_type == journal,
                                 Entry.status == "in_progress", Entry.started_at < cutoff).all()
    for entry in old:
        remove_entry(db, entry)
    db.commit()
    return len(old)


def client_id_of(db: Session, entry: Entry) -> uuid.UUID:
    """A record from before the app owned its steps has no client id yet: it gets one, so it can be carried on."""
    if entry.client_id is None:
        entry.client_id = uuid.uuid4()
        db.commit()
    return entry.client_id


def day_of(entry: Entry, tz_offset: int) -> date:
    if entry.local_date is not None:
        return entry.local_date
    offset = entry.tz_offset if entry.tz_offset is not None else tz_offset
    return insights.local_date(entry.completed_at or entry.started_at, offset)


def source_feelings(db: Session, account_id, source_id, journals: tuple[str, ...], message: str) -> list[str]:
    """The feelings of the entry a record is opened from (one of `journals`), to start ticked."""
    source = db.query(Entry).filter(Entry.id == source_id, Entry.account_id == account_id).first()
    if source is None or source.journal_type not in journals:
        raise Refused("bad_source", message)
    row = db.query(CapturedValue).filter(CapturedValue.entry_id == source.id, CapturedValue.key == "feelings").first()
    return json.loads(row.value) if row and row.value else []
