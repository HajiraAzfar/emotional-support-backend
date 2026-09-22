import uuid
from datetime import datetime
from sqlalchemy import Column, String, DateTime, Integer, ForeignKey, ARRAY
from sqlalchemy.dialects.postgresql import UUID
from app.core.database import Base


class Entry(Base):
    __tablename__ = "entries"

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
    # Highest crisis tier assigned within this entry; danger/emergency suppress all AI for it (FR-CRIS-006/007).
    crisis_tier = Column(String(12), nullable=True)
    started_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    completed_at = Column(DateTime, nullable=True)