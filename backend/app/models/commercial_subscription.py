from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String

from app.database.connection import Base


class CommercialSubscription(Base):
    __tablename__ = "commercial_subscriptions"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, unique=True, index=True)
    plan = Column(String, nullable=False, default="FREE")
    status = Column(String, nullable=False, default="LAUNCH_ACCESS")
    provider = Column(String, nullable=False, default="MOCK")
    reference_price = Column(String, nullable=False, default="MX$ 0 / mês")
    started_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    expires_at = Column(DateTime, nullable=True)
    external_reference = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=True)


class PlanChangeEvent(Base):
    __tablename__ = "plan_change_events"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    actor_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    previous_plan = Column(String, nullable=False)
    new_plan = Column(String, nullable=False)
    provider = Column(String, nullable=False, default="MOCK")
    reason = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
