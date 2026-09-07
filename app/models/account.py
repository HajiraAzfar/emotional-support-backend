import uuid
from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String
from sqlalchemy.dialects.postgresql import UUID

from app.core.database import Base


class Account(Base):
    __tablename__ = "accounts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String(255), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    verified = Column(Boolean, default=False, nullable=False)
    weekly_goal = Column(Integer, default=3, nullable=False)
    interface_language = Column(String(10), default="en", nullable=False)
    consent_version = Column(String(20), nullable=True)
    consent_at = Column(DateTime, nullable=True)
    distress_baseline = Column(Integer, nullable=True)
    distress_baseline_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)