import uuid
from datetime import datetime
from sqlalchemy import Column, String, Text, DateTime, Integer, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from app.core.database import Base


class Message(Base):
    __tablename__ = "messages"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entry_id = Column(UUID(as_uuid=True), ForeignKey("entries.id", ondelete="CASCADE"), nullable=False, index=True)
    role = Column(String(20), nullable=False)  # "ai" or "user"
    # capture_prompt | capture_answer | offer | chat | crisis
    kind = Column(String(20), nullable=True)
    # Set on capture prompts and answers: which scheduled value they carry (FR-ENT-026).
    value_id = Column(String(50), nullable=True)
    content = Column(Text, nullable=False)
    sequence = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)