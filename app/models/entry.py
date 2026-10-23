import uuid
from datetime import datetime
from sqlalchemy import Boolean, Column, Date, String, DateTime, Index, Integer, ForeignKey, ARRAY, text
from sqlalchemy.dialects.postgresql import UUID
from app.core.database import Base


class Entry(Base):
    __tablename__ = "entries"
    # One check-in per slot per day (core/slots.py). Only slotted check-ins are
    # bound by it; every other entry, and every check-in from before slots, has
    # no slot.
    __table_args__ = (
        Index(
            "uq_entries_checkin_slot", "account_id", "local_date", "slot", unique=True,
            postgresql_where=text("slot IS NOT NULL"), sqlite_where=text("slot IS NOT NULL"),
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id = Column(UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    journal_type = Column(String(50), nullable=False)
    domains = Column(ARRAY(String), nullable=True)
    status = Column(String(20), default="in_progress", nullable=False)
    current_step = Column(Integer, nullable=True)
    # None until capture finishes, then: offered | declined | active | closed
    conversation_status = Column(String(20), nullable=True)
    # Recorded, never shown to the user (FR-AIR-012).
    closure_reason = Column(String(40), nullable=True)
    # Where the conversation is: exploring, or confirming a shift the user reached
    # (one gentle consolidating turn before the closing message).
    conversation_stage = Column(String(20), nullable=True)
    # Highest crisis tier assigned within this entry; danger/emergency suppress all AI for it (FR-CRIS-006/007).
    crisis_tier = Column(String(12), nullable=True)
        # FR-JRN-006: the cycle this one continues, so a further cycle can reuse the feared outcome.
    parent_entry_id = Column(UUID(as_uuid=True), ForeignKey("entries.id", ondelete="SET NULL"), nullable=True)
    # FR-JRN-007: the scope notice must be acknowledged before the first value is requested.
    notice_acknowledged = Column(Boolean, default=False, nullable=False)
    # Daily check-in: the id the app made for this check-in, so a retry after a
    # lost response returns the same entry instead of saving it twice.
    client_id = Column(UUID(as_uuid=True), nullable=True, unique=True)
    # Check-in slot: morning | afternoon | evening (core/slots.py).
    slot = Column(String(12), nullable=True)
    # The user's own calendar day the slot belongs to. An evening slot runs past
    # midnight and stays that evening's day.
    local_date = Column(Date, nullable=True)
    # The phone's clock when the entry was made: minutes east of UTC, and the
    # IANA name it reported. started_at and completed_at stay in UTC.
    tz_offset = Column(Integer, nullable=True)
    timezone = Column(String(64), nullable=True)
    # The check-in a journal was opened from, so it can start from its feelings.
    source_entry_id = Column(UUID(as_uuid=True), ForeignKey("entries.id", ondelete="SET NULL"), nullable=True)
    started_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    completed_at = Column(DateTime, nullable=True)