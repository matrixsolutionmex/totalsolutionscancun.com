from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint

from app.database.connection import Base


class ExternalPaymentConfirmation(Base):
    __tablename__ = "external_payment_confirmations"
    __table_args__ = (
        UniqueConstraint("organization_id", "external_reference", name="uq_external_payment_confirmation_reference"),
        UniqueConstraint("payment_id", name="uq_external_payment_confirmation_payment"),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    service_order_id = Column(Integer, ForeignKey("service_orders.id"), nullable=False, index=True)
    payment_id = Column(Integer, ForeignKey("payments.id"), nullable=False, index=True)
    purpose = Column(String(32), nullable=False, index=True)
    payment_method = Column(String(32), nullable=False)
    amount = Column(Numeric(12, 2), nullable=False)
    currency = Column(String(8), nullable=False)
    external_reference = Column(String(120), nullable=False)
    evidence_reference = Column(String(240), nullable=True)
    observation = Column(Text, nullable=True)
    confirmed_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    confirmed_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
