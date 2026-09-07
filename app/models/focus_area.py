import uuid

from sqlalchemy import Column, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class FocusArea(Base):
    __tablename__ = "focus_areas"
    __table_args__ = (UniqueConstraint("account_id", "code", name="uq_account_focus_area"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id = Column(UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    code = Column(String(50), nullable=False)