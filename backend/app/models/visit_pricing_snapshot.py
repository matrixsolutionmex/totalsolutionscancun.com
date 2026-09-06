from datetime import datetime
from decimal import Decimal

from sqlalchemy import Column, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint

from app.database.connection import Base


class VisitPricingSnapshot(Base):
    __tablename__ = "visit_pricing_snapshots"
    __table_args__ = (UniqueConstraint("service_order_id", name="uq_visit_pricing_snapshot_order"),)

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    service_order_id = Column(Integer, ForeignKey("service_orders.id"), nullable=False, index=True)
    base_price = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    zone_fee = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    distance_fee = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    urgency_fee = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    after_hours_fee = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    discount_amount = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    tax_amount = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    total_amount = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    currency = Column(String(8), nullable=False, default="MXN")
    pricing_version = Column(String(40), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
