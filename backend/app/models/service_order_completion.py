from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint

from app.database.connection import Base


class ServiceOrderTechnicalCompletion(Base):
    __tablename__ = "service_order_technical_completions"
    __table_args__ = (UniqueConstraint("service_order_id", name="uq_technical_completion_order"),)

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    service_order_id = Column(Integer, ForeignKey("service_orders.id"), nullable=False, index=True)
    status = Column(String(24), nullable=False, default="REPORTED", index=True)
    completion_notes = Column(Text, nullable=True)
    final_observation = Column(Text, nullable=True)
    evidence_reference = Column(String(240), nullable=True)
    responsible_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    technical_completed_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    reviewed_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    reviewed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class ServiceOrderCustomerAcceptance(Base):
    __tablename__ = "service_order_customer_acceptances"
    __table_args__ = (UniqueConstraint("service_order_id", name="uq_customer_acceptance_order"),)

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    service_order_id = Column(Integer, ForeignKey("service_orders.id"), nullable=False, index=True)
    status = Column(String(24), nullable=False, default="PENDING", index=True)
    accepted_at = Column(DateTime, nullable=True)
    accepted_source = Column(String(32), nullable=True)
    problem_reason = Column(Text, nullable=True)
    problem_reported_at = Column(DateTime, nullable=True)
    idempotency_key = Column(String(128), nullable=True, unique=True, index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class ServiceOrderWarranty(Base):
    __tablename__ = "service_order_warranties"
    __table_args__ = (UniqueConstraint("service_order_id", name="uq_service_order_warranty_order"),)

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    service_order_id = Column(Integer, ForeignKey("service_orders.id"), nullable=False, index=True)
    customer_lead_id = Column(Integer, ForeignKey("leads.id"), nullable=True, index=True)
    status = Column(String(24), nullable=False, default="ACTIVE", index=True)
    warranty_days = Column(Integer, nullable=False)
    starts_at = Column(DateTime, nullable=False)
    ends_at = Column(DateTime, nullable=False)
    scope = Column(Text, nullable=True)
    source = Column(String(32), nullable=False, default="CUSTOMER_ACCEPTANCE")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
