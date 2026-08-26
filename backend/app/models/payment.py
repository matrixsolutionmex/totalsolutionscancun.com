from datetime import datetime
from decimal import Decimal

from sqlalchemy import Column, DateTime, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import relationship

from app.database.connection import Base


class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    service_request_id = Column(Integer, ForeignKey("service_requests.id"), nullable=True, index=True)
    service_order_id = Column(Integer, ForeignKey("service_orders.id"), nullable=True, index=True)
    lead_id = Column(Integer, ForeignKey("leads.id"), nullable=True, index=True)
    technician_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    upgrade_intent_id = Column(Integer, ForeignKey("commercial_upgrade_intents.id"), nullable=True, index=True)

    payment_type = Column(String(40), nullable=False, index=True)
    payment_method = Column(String(40), nullable=False, index=True)
    currency = Column(String(8), nullable=False, default="mxn")
    gross_amount = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    platform_fee_amount = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    processor_fee_amount = Column(Numeric(12, 2), nullable=True)
    provider_amount = Column(Numeric(12, 2), nullable=True)
    status = Column(String(32), nullable=False, default="PENDING", index=True)
    provider = Column(String(32), nullable=False, default="STRIPE")
    checkout_url = Column(String, nullable=True)
    stripe_checkout_session_id = Column(String, nullable=True, unique=True, index=True)
    stripe_payment_intent_id = Column(String, nullable=True, index=True)
    stripe_customer_id = Column(String, nullable=True, index=True)
    stripe_subscription_id = Column(String, nullable=True, index=True)
    stripe_price_id = Column(String, nullable=True)
    idempotency_key = Column(String, nullable=False, unique=True, index=True)
    paid_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    upgrade_intent = relationship("CommercialUpgradeIntent", back_populates="payment")


class PlatformLedgerEntry(Base):
    __tablename__ = "platform_ledger_entries"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    technician_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    service_order_id = Column(Integer, ForeignKey("service_orders.id"), nullable=True, index=True)
    payment_id = Column(Integer, ForeignKey("payments.id"), nullable=True, index=True)
    entry_type = Column(String(32), nullable=False, index=True)
    amount = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    currency = Column(String(8), nullable=False, default="mxn")
    status = Column(String(24), nullable=False, default="OPEN")
    description = Column(String(240), nullable=True)
    reference = Column(String(120), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    settled_at = Column(DateTime, nullable=True)
