from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint

from app.database.connection import Base


class TechnicianEarningReconciliationEvent(Base):
    """Append-only provider/payment facts used to make earning recognition idempotent."""

    __tablename__ = "technician_earning_reconciliation_events"
    __table_args__ = (
        UniqueConstraint("provider_event_key_hash", name="uq_technician_earning_reconciliation_key"),
        CheckConstraint(
            "event_type IN ('PAYMENT_CONFIRMED', 'REFUND', 'DISPUTE_OPEN', 'DISPUTE_WON', 'DISPUTE_LOST')",
            name="ck_technician_earning_reconciliation_type",
        ),
        CheckConstraint("amount >= 0", name="ck_technician_earning_reconciliation_amount"),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    service_order_id = Column(Integer, ForeignKey("service_orders.id"), nullable=False, index=True)
    technician_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    payment_id = Column(Integer, ForeignKey("payments.id"), nullable=False, index=True)
    installment_id = Column(Integer, ForeignKey("service_order_payment_installments.id"), nullable=True, index=True)
    earning_id = Column(Integer, ForeignKey("technician_earnings.id"), nullable=True, index=True)
    reversal_of_earning_id = Column(Integer, ForeignKey("technician_earnings.id"), nullable=True, index=True)
    provider_event_key_hash = Column(String(64), nullable=False, unique=True, index=True)
    event_type = Column(String(32), nullable=False, index=True)
    amount = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    currency = Column(String(8), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
