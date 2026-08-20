from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String

from app.database.connection import Base


class UserCommercialProfile(Base):
    """Optional user-level entitlement override inside an organization."""

    __tablename__ = "user_commercial_profiles"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, unique=True, index=True)
    plan = Column(String(20), nullable=False, default="FREE")
    status = Column(String(40), nullable=False, default="ACTIVE")
    source = Column(String(40), nullable=False, default="ONBOARDING")
    granted_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
