from datetime import datetime
from decimal import Decimal

from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, Numeric, String, UniqueConstraint
from sqlalchemy.orm import relationship

from app.database.connection import Base


class ServiceOrderPaymentPlan(Base):
    __tablename__ = "service_order_payment_plans"
    __table_args__ = (UniqueConstraint("service_order_id", "quote_id", "quote_version", name="uq_service_order_payment_plan_quote"),)

    id = Column(Integer, primary_key=True, index=True)
    service_order_id = Column(Integer, ForeignKey("service_orders.id"), nullable=False, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    quote_id = Column(Integer, ForeignKey("service_order_quotes.id"), nullable=False, index=True)
    quote_version = Column(Integer, nullable=False)
    currency = Column(String(8), nullable=False, default="MXN")
    approved_total = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    policy_snapshot = Column(JSON, nullable=False)
    status = Column(String(24), nullable=False, default="ACTIVE", index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    installments = relationship("ServiceOrderPaymentInstallment", back_populates="payment_plan", cascade="all, delete-orphan", order_by="ServiceOrderPaymentInstallment.sequence")


class ServiceOrderPaymentInstallment(Base):
    __tablename__ = "service_order_payment_installments"
    __table_args__ = (UniqueConstraint("payment_plan_id", "sequence", name="uq_service_order_payment_installment_sequence"),)

    id = Column(Integer, primary_key=True, index=True)
    payment_plan_id = Column(Integer, ForeignKey("service_order_payment_plans.id", ondelete="CASCADE"), nullable=False, index=True)
    service_order_id = Column(Integer, ForeignKey("service_orders.id"), nullable=False, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    sequence = Column(Integer, nullable=False)
    installment_type = Column(String(24), nullable=False)
    percentage = Column(Numeric(6, 2), nullable=False)
    amount = Column(Numeric(12, 2), nullable=False)
    currency = Column(String(8), nullable=False, default="MXN")
    status = Column(String(24), nullable=False, default="PENDING", index=True)
    due_trigger = Column(String(32), nullable=False, default="EXPLICIT_RELEASE")
    payment_id = Column(Integer, ForeignKey("payments.id"), nullable=True, index=True)
    paid_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    payment_plan = relationship("ServiceOrderPaymentPlan", back_populates="installments")
