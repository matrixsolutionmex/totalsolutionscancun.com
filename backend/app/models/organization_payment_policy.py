from datetime import datetime
from decimal import Decimal

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, Numeric, String

from app.database.connection import Base


class OrganizationPaymentPolicy(Base):
    __tablename__ = "organization_payment_policies"

    organization_id = Column(Integer, ForeignKey("organizations.id"), primary_key=True)
    visit_required = Column(Boolean, nullable=False, default=True)
    visit_payment_timing = Column(String(20), nullable=False, default="PREPAID")
    allow_card = Column(Boolean, nullable=False, default=True)
    allow_cash = Column(Boolean, nullable=False, default=True)
    allow_bank_transfer = Column(Boolean, nullable=False, default=True)
    private_marketplace_fee_rate = Column(Numeric(6, 3), nullable=False, default=Decimal("0"))
    marketplace_fee_rate = Column(Numeric(6, 3), nullable=False, default=Decimal("10"))
    small_service_limit = Column(Numeric(12, 2), nullable=False, default=Decimal("3000"))
    medium_service_limit = Column(Numeric(12, 2), nullable=False, default=Decimal("15000"))
    medium_deposit_percentage = Column(Numeric(6, 3), nullable=False, default=Decimal("30"))
    large_stage_1_percentage = Column(Numeric(6, 3), nullable=False, default=Decimal("30"))
    large_stage_2_percentage = Column(Numeric(6, 3), nullable=False, default=Decimal("40"))
    large_stage_3_percentage = Column(Numeric(6, 3), nullable=False, default=Decimal("30"))
    refund_before_dispatch_percentage = Column(Numeric(6, 3), nullable=False, default=Decimal("100"))
    refund_after_dispatch_percentage = Column(Numeric(6, 3), nullable=False, default=Decimal("0"))
    currency = Column(String(8), nullable=False, default="MXN")
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
