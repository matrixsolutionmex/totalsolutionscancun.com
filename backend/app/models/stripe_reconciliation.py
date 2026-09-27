from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint

from app.database.connection import Base


class StripeWebhookEvent(Base):
    __tablename__ = "stripe_webhook_events"
    __table_args__ = (UniqueConstraint("event_id", name="uq_stripe_webhook_event_id"),)

    id = Column(Integer, primary_key=True, index=True)
    event_id = Column(String(255), nullable=False, index=True)
    event_type = Column(String(96), nullable=False, index=True)
    livemode = Column(Boolean, nullable=True, index=True)
    object_id = Column(String(255), nullable=True, index=True)
    payment_id = Column(Integer, ForeignKey("payments.id"), nullable=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=True, index=True)
    status = Column(String(24), nullable=False, default="PROCESSING", index=True)
    attempts = Column(Integer, nullable=False, default=1)
    received_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    processed_at = Column(DateTime, nullable=True)
    last_error = Column(String(240), nullable=True)


class StripePaymentAdjustment(Base):
    __tablename__ = "stripe_payment_adjustments"
    __table_args__ = (
        UniqueConstraint("kind", "provider_adjustment_id", name="uq_stripe_payment_adjustment_provider"),
        UniqueConstraint("stripe_event_id", name="uq_stripe_payment_adjustment_event"),
    )

    id = Column(Integer, primary_key=True, index=True)
    payment_id = Column(Integer, ForeignKey("payments.id"), nullable=False, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    stripe_event_id = Column(String(255), nullable=False, index=True)
    provider_adjustment_id = Column(String(255), nullable=False, index=True)
    kind = Column(String(24), nullable=False, index=True)
    status = Column(String(32), nullable=False, index=True)
    amount = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    currency = Column(String(8), nullable=False)
    ledger_idempotency_key = Column(String(255), nullable=True, unique=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
