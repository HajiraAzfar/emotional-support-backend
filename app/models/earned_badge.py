import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class EarnedBadge(Base):
    """
    One badge, earned once. Badges recognise what she did — wrote a thought
    record, saved a moment, noticed a pattern twice — never how she felt, and
    never a streak she could break.

    The unique constraint is what makes awarding idempotent: the evaluator runs
    on every weekly report and simply re-inserts, so a report regenerated after
    an edit cannot award the same badge twice.
    """
    __tablename__ = "earned_badges"
    __table_args__ = (UniqueConstraint("account_id", "badge_id", name="uq_badge_per_account"),)

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id = Column(UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    # The id in content/badges.json. Definitions live there, not here, so the
    # wording can change without a migration.
    badge_id = Column(String(40), nullable=False)
    earned_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    # The entries that qualified her, comma-separated, so "how did I earn this?"
    # can be answered by opening them. Entries may later be deleted; a missing
    # one is simply not shown.
    entry_ids = Column(Text, nullable=True)
