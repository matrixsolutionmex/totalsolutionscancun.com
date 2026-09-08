from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint

from app.database.connection import Base


class ServiceOrderInstallmentReleaseEvent(Base):
    __tablename__ = "service_order_installment_release_events"
    __table_args__ = (UniqueConstraint("installment_id", name="uq_installment_release_event_installment"),)

    id = Column(Integer, primary_key=True, index=True)
    installment_id = Column(Integer, ForeignKey("service_order_payment_installments.id"), nullable=False, index=True)
    service_order_id = Column(Integer, ForeignKey("service_orders.id"), nullable=False, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    previous_status = Column(String(24), nullable=False)
    new_status = Column(String(24), nullable=False)
    trigger_type = Column(String(48), nullable=False)
    actor_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    actor_role = Column(String(24), nullable=False)
    observation = Column(Text, nullable=True)
    evidence_reference = Column(String(240), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
