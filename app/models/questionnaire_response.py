import uuid
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class QuestionnaireResponse(Base):
    """
    FR-INS-017/018: one offer of the wellbeing questionnaire, either completed
    or declined. Declines are rows too, because the next offer is due fourteen
    days after the last of either.

    The score is stored and never shown to the user (FR-INS-018); the answers
    themselves are kept so a future instrument change can be reasoned about.
    """
    __tablename__ = "questionnaire_responses"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id = Column(UUID(as_uuid=True), ForeignKey("accounts.id", ondelete="CASCADE"), nullable=False, index=True)
    # completed | declined
    outcome = Column(String(12), nullable=False)
    # Which set of questions was answered, so old scores are never compared with new ones.
    version = Column(String(20), nullable=False)
    score = Column(Integer, nullable=True)
    max_score = Column(Integer, nullable=True)
    # JSON of {item id: value}, for a later review of the instrument.
    answers = Column(String(500), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
