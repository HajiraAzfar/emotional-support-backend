import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class LibraryFavourite(Base):
    """SRS 4.11: an article she saved. The slug is the article's file name."""
    __tablename__ = "library_favourites"
    __table_args__ = (UniqueConstraint("account_id", "slug", name="uq_library_favourite"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id = Column(UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    slug = Column(String(80), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
