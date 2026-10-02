from datetime import datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint

from app.database.connection import Base


EARNING_STATUSES = frozenset({
    "PROCESSING",
    "IN_GUARANTEE",
    "AVAILABLE_FOR_PAYMENT",
    "PAYMENT_REQUESTED",
    "PAID",
    "HELD",
    "REVERSED",
})
EARNING_EVENT_TYPES = frozenset({
    "CREATED", "STATUS_CHANGED", "REVERSED", "EARNING_RECOGNIZED", "EARNING_ADJUSTED",
    "EARNING_REVERSED", "GUARANTEE_STARTED", "AVAILABLE_FOR_PAYMENT",
})
EARNING_REASON_CODES = frozenset({
    "EARNING_CREATED",
    "ELIGIBILITY_CONFIRMED",
    "GUARANTEE_CLOSED",
    "REFUND_OR_DISPUTE",
    "IDENTITY_SUSPENDED",
    "ADMINISTRATIVE_HOLD",
    "CORRECTION",
    "PAYMENT_CONFIRMED",
    "REFUND_OR_DISPUTE_ADJUSTMENT",
    "GUARANTEE_STARTED",
    "AVAILABLE_FOR_PAYMENT",
})


class TechnicianEarning(Base):
    __tablename__ = "technician_earnings"
    __table_args__ = (
        UniqueConstraint("idempotency_key_hash", name="uq_technician_earning_idempotency_hash"),
        CheckConstraint(
            "status IN ('PROCESSING', 'IN_GUARANTEE', 'AVAILABLE_FOR_PAYMENT', 'PAYMENT_REQUESTED', 'PAID', 'HELD', 'REVERSED')",
            name="ck_technician_earning_status",
        ),
        CheckConstraint("gross_amount >= 0 AND platform_fee_amount >= 0 AND processing_fee_amount >= 0", name="ck_technician_earning_nonnegative_amounts"),
        CheckConstraint("net_amount = gross_amount - platform_fee_amount - processing_fee_amount", name="ck_technician_earning_net_consistent"),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    service_order_id = Column(Integer, ForeignKey("service_orders.id"), nullable=False, index=True)
    technician_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    payment_id = Column(Integer, ForeignKey("payments.id"), nullable=True, index=True)
    currency = Column(String(8), nullable=False, default="MXN")
    gross_amount = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    platform_fee_amount = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    processing_fee_amount = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    net_amount = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    policy_version = Column(String(64), nullable=False)
    source_type = Column(String(40), nullable=False)
    source_reference = Column(String(160), nullable=False)
    status = Column(String(32), nullable=False, default="PROCESSING", index=True)
    reversal_of_id = Column(Integer, ForeignKey("technician_earnings.id"), nullable=True, index=True)
    idempotency_key_hash = Column(String(64), nullable=False, unique=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class TechnicianEarningEvent(Base):
    __tablename__ = "technician_earning_events"
    __table_args__ = (
        UniqueConstraint("idempotency_key_hash", name="uq_technician_earning_event_idempotency_hash"),
        CheckConstraint(
            "event_type IN ('CREATED', 'STATUS_CHANGED', 'REVERSED', 'EARNING_RECOGNIZED', 'EARNING_ADJUSTED', 'EARNING_REVERSED', 'GUARANTEE_STARTED', 'AVAILABLE_FOR_PAYMENT')",
            name="ck_technician_earning_event_type",
        ),
        CheckConstraint(
            "reason_code IN ('EARNING_CREATED', 'ELIGIBILITY_CONFIRMED', 'GUARANTEE_CLOSED', 'REFUND_OR_DISPUTE', 'IDENTITY_SUSPENDED', 'ADMINISTRATIVE_HOLD', 'CORRECTION', 'PAYMENT_CONFIRMED', 'REFUND_OR_DISPUTE_ADJUSTMENT', 'GUARANTEE_STARTED', 'AVAILABLE_FOR_PAYMENT')",
            name="ck_technician_earning_event_reason_code",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    earning_id = Column(Integer, ForeignKey("technician_earnings.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    event_type = Column(String(32), nullable=False, index=True)
    previous_status = Column(String(32), nullable=True)
    new_status = Column(String(32), nullable=False)
    actor_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    reason_code = Column(String(64), nullable=False)
    idempotency_key_hash = Column(String(64), nullable=False, unique=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
