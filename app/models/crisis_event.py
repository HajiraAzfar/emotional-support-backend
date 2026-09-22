import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class CrisisEvent(Base):
    """
    FR-CRIS-013: one row per non-clear tier assignment. Holds the tier, the
    field, the entry and the time — never the evaluated text.
    """
    __tablename__ = "crisis_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id = Column(UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    # SET NULL: deleting an entry removes its content, not the (content-free) detection record.
    entry_id = Column(UUID(as_uuid=True), ForeignKey("entries.id", ondelete="SET NULL"), nullable=True, index=True)
    field = Column(String(50), nullable=False)
    tier = Column(String(12), nullable=False)
    # Which crisis content was shown: full | abbreviated | None (mild shows no interruption).
    variant = Column(String(12), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
