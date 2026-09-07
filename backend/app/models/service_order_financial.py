from datetime import datetime
from decimal import Decimal

from sqlalchemy import Column, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint

from app.database.connection import Base


class ServiceOrderFinancial(Base):
    __tablename__ = "service_order_financials"
    __table_args__ = (UniqueConstraint("service_order_id", name="uq_service_order_financial_order"),)

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    service_order_id = Column(Integer, ForeignKey("service_orders.id"), nullable=False, index=True)
    order_origin = Column(String(32), nullable=False, default="PRIVATE")
    currency = Column(String(8), nullable=False, default="MXN")
    visit_fee = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    approved_quote_amount = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    service_paid_amount = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    service_outstanding_balance = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    amount_due = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    amount_paid = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    amount_refunded = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    outstanding_balance = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    platform_fee_amount = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    provider_earning_amount = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    processing_fee_amount = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    financial_status = Column(String(32), nullable=False, default="NO_CHARGE", index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
