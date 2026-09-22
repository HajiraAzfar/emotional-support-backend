import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class UserTerm(Base):
    """FR-PICK-006: a user's own addition to the feelings or triggers library."""
    __tablename__ = "user_terms"
    __table_args__ = (UniqueConstraint("account_id", "library", "name", name="uq_user_term"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id = Column(UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    library = Column(String(30), nullable=False)
    name = Column(String(20), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

    @property
    def item_id(self) -> str:
        """Id used inside captured selections, distinct from predefined ids."""
        return f"u_{self.id.hex}"
