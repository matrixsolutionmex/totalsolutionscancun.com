from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint

from app.database.connection import Base


COMPENSATION_POLICY_STATUSES = frozenset({"DRAFT", "ACTIVE", "RETIRED", "VOID"})
COMPENSATION_SNAPSHOT_STATUSES = frozenset({"PROPOSED", "FROZEN", "VOID"})
COMPENSATION_ITEM_CATEGORIES = frozenset({"LABOR", "MATERIAL", "TAX", "REIMBURSEMENT", "OTHER"})
COMPENSATION_INSTALLMENT_RULE = "PROPORTIONAL_CONFIRMED_INSTALLMENTS"


class TechnicianCompensationPolicy(Base):
    __tablename__ = "technician_compensation_policies"
    __table_args__ = (
        UniqueConstraint("idempotency_key_hash", name="uq_compensation_policy_idempotency_hash"),
        UniqueConstraint("organization_id", "currency", "version", name="uq_compensation_policy_org_currency_version"),
        CheckConstraint("status IN ('DRAFT', 'ACTIVE', 'RETIRED', 'VOID')", name="ck_compensation_policy_status"),
        CheckConstraint("technician_share_bps >= 0 AND organization_share_bps >= 0", name="ck_compensation_policy_nonnegative_bps"),
        CheckConstraint("technician_share_bps + organization_share_bps = 10000", name="ck_compensation_policy_bps_total"),
        CheckConstraint("default_guarantee_days >= 0", name="ck_compensation_policy_guarantee_days"),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    version = Column(Integer, nullable=False)
    status = Column(String(16), nullable=False, default="DRAFT", index=True)
    currency = Column(String(8), nullable=False, default="MXN")
    technician_share_bps = Column(Integer, nullable=False, default=7500)
    organization_share_bps = Column(Integer, nullable=False, default=2500)
    default_guarantee_days = Column(Integer, nullable=False, default=7)
    installment_rule = Column(String(64), nullable=False, default="PROPORTIONAL_CONFIRMED_INSTALLMENTS")
    processor_fee_responsibility = Column(String(24), nullable=False, default="ORGANIZATION")
    discount_rule = Column(String(32), nullable=False, default="ORGANIZATION_SHARE_ONLY")
    effective_from = Column(DateTime, nullable=False)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=False)
    activated_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    idempotency_key_hash = Column(String(64), nullable=False, unique=True, index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class ServiceOrderCompensationSnapshot(Base):
    __tablename__ = "service_order_compensation_snapshots"
    __table_args__ = (
        UniqueConstraint("idempotency_key_hash", name="uq_compensation_snapshot_idempotency_hash"),
        CheckConstraint("status IN ('PROPOSED', 'FROZEN', 'VOID')", name="ck_compensation_snapshot_status"),
        CheckConstraint("labor_base_amount >= 0 AND material_amount >= 0 AND tax_amount >= 0 AND reimbursement_amount >= 0 AND discount_amount >= 0", name="ck_compensation_snapshot_nonnegative_amounts"),
        CheckConstraint("technician_amount >= 0 AND organization_amount >= 0", name="ck_compensation_snapshot_share_amounts"),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    service_order_id = Column(Integer, ForeignKey("service_orders.id"), nullable=False, index=True)
    technician_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    policy_id = Column(Integer, ForeignKey("technician_compensation_policies.id"), nullable=False, index=True)
    policy_version = Column(Integer, nullable=False)
    currency = Column(String(8), nullable=False)
    labor_base_amount = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    material_amount = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    tax_amount = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    reimbursement_amount = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    discount_amount = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    technician_share_bps = Column(Integer, nullable=False)
    organization_share_bps = Column(Integer, nullable=False)
    technician_amount = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    organization_amount = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    guarantee_days = Column(Integer, nullable=False)
    installment_rule = Column(String(64), nullable=False)
    status = Column(String(16), nullable=False, default="PROPOSED", index=True)
    approved_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    frozen_at = Column(DateTime, nullable=True)
    idempotency_key_hash = Column(String(64), nullable=False, unique=True, index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class TechnicianCompensationEvent(Base):
    __tablename__ = "technician_compensation_events"
    __table_args__ = (
        UniqueConstraint("idempotency_key_hash", name="uq_compensation_event_idempotency_hash"),
        CheckConstraint("event_type IN ('POLICY_CREATED', 'POLICY_ACTIVATED', 'POLICY_VOIDED', 'SNAPSHOT_PROPOSED', 'SNAPSHOT_FROZEN', 'SNAPSHOT_VOIDED')", name="ck_compensation_event_type"),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    policy_id = Column(Integer, ForeignKey("technician_compensation_policies.id"), nullable=True, index=True)
    snapshot_id = Column(Integer, ForeignKey("service_order_compensation_snapshots.id"), nullable=True, index=True)
    event_type = Column(String(32), nullable=False, index=True)
    actor_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    reason_code = Column(String(64), nullable=False)
    idempotency_key_hash = Column(String(64), nullable=False, unique=True, index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
