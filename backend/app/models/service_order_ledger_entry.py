from datetime import datetime
from decimal import Decimal

from sqlalchemy import Column, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint

from app.database.connection import Base


class ServiceOrderLedgerEntry(Base):
    __tablename__ = "service_order_ledger_entries"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_service_order_ledger_idempotency"),)

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    service_order_id = Column(Integer, ForeignKey("service_orders.id"), nullable=False, index=True)
    entry_type = Column(String(32), nullable=False, index=True)
    amount = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    currency = Column(String(8), nullable=False, default="MXN")
    payment_method = Column(String(32), nullable=True)
    payment_id = Column(Integer, ForeignKey("payments.id"), nullable=True, index=True)
    actor_type = Column(String(32), nullable=True)
    actor_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    status = Column(String(24), nullable=False, default="CONFIRMED", index=True)
    external_reference = Column(String(120), nullable=True)
    idempotency_key = Column(String(180), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
