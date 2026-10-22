import uuid
from datetime import datetime

from sqlalchemy import Column, Date, DateTime, ForeignKey, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class WeeklyReflection(Base):
    """
    "Carry into next week": one line she chose to take forward, shown again at
    the top of the following week's report.

    It is hers and optional — nothing is created unless she writes something,
    and no reminder or goal is made from it. The week is stored as its local
    Monday, so the row belongs to the report she was looking at whatever
    timezone she reads it in later.
    """
    __tablename__ = "weekly_reflections"
    __table_args__ = (UniqueConstraint("account_id", "week_start", name="uq_reflection_per_week"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id = Column(UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    week_start = Column(Date, nullable=False)
    text = Column(Text, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
