from datetime import datetime
from decimal import Decimal

from sqlalchemy import Column, DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import relationship

from app.database.connection import Base


class CommercialUpgradeIntent(Base):
    __tablename__ = "commercial_upgrade_intents"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    requested_plan = Column(String(20), nullable=False, index=True)
    reference_price_mxn = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    provider = Column(String(40), nullable=False, default="CLIP")
    status = Column(String(40), nullable=False, default="CHECKOUT_OPENED", index=True)
    source = Column(String(80), nullable=False, default="PLANS_UI")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    activated_at = Column(DateTime, nullable=True)
    activated_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    payment_confirmed_at = Column(DateTime, nullable=True)
    payment_confirmed_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    confirmation_source = Column(String(40), nullable=True)

    payment = relationship("Payment", back_populates="upgrade_intent", uselist=False)
