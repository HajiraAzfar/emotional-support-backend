import uuid
from datetime import datetime
from sqlalchemy import Column, String, Text, DateTime, Integer, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from app.core.database import Base


class Message(Base):
    __tablename__ = "messages"
    # A thread's positions are 1, 2, 3… with no duplicates, even on a double tap.
    __table_args__ = (UniqueConstraint("entry_id", "sequence", name="uq_message_position"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    entry_id = Column(UUID(as_uuid=True), ForeignKey("entries.id", ondelete="CASCADE"), nullable=False, index=True)
    role = Column(String(20), nullable=False)  # "ai" or "user"
    # capture_prompt | capture_answer | offer | chat | crisis
    kind = Column(String(20), nullable=True)
    # Set on capture prompts and answers: which scheduled value they carry (FR-ENT-026).
    value_id = Column(String(50), nullable=True)
    content = Column(Text, nullable=False)
    # AI Chat: the helplines card drawn under this reply (soft | prominent). Its own
    # field, never part of the text, so no app build can show the label as words.
    card = Column(String(10), nullable=True)
    sequence = Column(Integer, nullable=False)
    # Journals: the id the app made for the answer this message carries, so a
    # retry after a lost response is recorded once.
    client_id = Column(UUID(as_uuid=True), nullable=True, unique=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)