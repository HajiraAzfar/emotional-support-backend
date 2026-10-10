import uuid
from datetime import datetime
from sqlalchemy import Boolean, Column, String, Text, DateTime, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from app.core.database import Base


class CapturedValue(Base):
    __tablename__ = "captured_values"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entry_id = Column(UUID(as_uuid=True), ForeignKey("entries.id", ondelete="CASCADE"), nullable=False, index=True)
    key = Column(String(100), nullable=False)
    # JSON-encoded typed value: integer, list of library ids, or text (FR-ENT-018).
    value = Column(Text, nullable=True)
    skipped = Column(Boolean, default=False, nullable=False)
    # answered | skipped (asked, left empty: value null) | not_asked (the schedule's
    # condition left it out, e.g. intensity with no feelings named). Insights reads
    # only answered values, and can tell "left empty" from "never asked".
    status = Column(String(10), default="answered", nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)