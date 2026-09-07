from datetime import datetime
from decimal import Decimal

from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from app.database.connection import Base


class ServiceOrderQuote(Base):
    __tablename__ = "service_order_quotes"
    __table_args__ = (UniqueConstraint("service_order_id", "version", name="uq_service_order_quote_version"),)

    id = Column(Integer, primary_key=True, index=True)
    service_order_id = Column(Integer, ForeignKey("service_orders.id"), nullable=False, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    version = Column(Integer, nullable=False, default=1)
    status = Column(String(20), nullable=False, default="DRAFT", index=True)
    subtotal = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    discount_amount = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    tax_amount = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    total = Column(Numeric(12, 2), nullable=False, default=Decimal("0.00"))
    currency = Column(String(8), nullable=False, default="MXN")
    valid_until = Column(DateTime, nullable=True)
    notes = Column(Text, nullable=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
    approved_at = Column(DateTime, nullable=True)
    approved_source = Column(String(32), nullable=True)
    approved_total = Column(Numeric(12, 2), nullable=True)
    approved_snapshot = Column(JSON, nullable=True)
    rejected_at = Column(DateTime, nullable=True)
    rejection_reason = Column(Text, nullable=True)

    service_order = relationship("ServiceOrder")
    items = relationship("ServiceOrderQuoteItem", back_populates="quote", cascade="all, delete-orphan", order_by="ServiceOrderQuoteItem.sort_order, ServiceOrderQuoteItem.id")


class ServiceOrderQuoteItem(Base):
    __tablename__ = "service_order_quote_items"

    id = Column(Integer, primary_key=True, index=True)
    quote_id = Column(Integer, ForeignKey("service_order_quotes.id", ondelete="CASCADE"), nullable=False, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    description = Column(String(240), nullable=False)
    quantity = Column(Numeric(12, 3), nullable=False)
    unit = Column(String(32), nullable=False, default="unidad")
    unit_price = Column(Numeric(12, 2), nullable=False)
    subtotal = Column(Numeric(12, 2), nullable=False)
    sort_order = Column(Integer, nullable=False, default=0)

    quote = relationship("ServiceOrderQuote", back_populates="items")
