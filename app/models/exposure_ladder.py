import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, Text
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class ExposureLadder(Base):
    """A fear ladder (Exposure Journal): one fear, its steps from easy to hard."""
    __tablename__ = "exposure_ladders"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id = Column(UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    fear_name = Column(Text, nullable=False)
    # Fixed tag ids (libraries/exposure_tags.json), as a JSON list.
    tags = Column(Text, nullable=False, default="[]")
    # Kept, not deleted, once an exposure points at one of its steps.
    archived = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class ExposureLadderItem(Base):
    """One step of a ladder. Owned through its ladder; archived rather than deleted once an exposure uses it."""
    __tablename__ = "exposure_ladder_items"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    ladder_id = Column(UUID(as_uuid=True), ForeignKey("exposure_ladders.id", ondelete="CASCADE"), nullable=False, index=True)
    description = Column(Text, nullable=False)
    # How anxious she expects the step to make her, 0-100 (SUDS).
    expected_suds = Column(Integer, nullable=False)
    sort_order = Column(Integer, nullable=False, default=0)
    archived = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
