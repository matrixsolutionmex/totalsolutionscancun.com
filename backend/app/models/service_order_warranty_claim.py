from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint

from app.database.connection import Base


class ServiceOrderWarrantyClaim(Base):
    __tablename__ = "service_order_warranty_claims"
    __table_args__ = (UniqueConstraint("idempotency_key", name="uq_warranty_claim_idempotency"),)

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    warranty_id = Column(Integer, ForeignKey("service_order_warranties.id"), nullable=False, index=True)
    service_order_id = Column(Integer, ForeignKey("service_orders.id"), nullable=False, index=True)
    customer_lead_id = Column(Integer, ForeignKey("leads.id"), nullable=True, index=True)
    status = Column(String(32), nullable=False, default="OPEN", index=True)
    reason = Column(String(240), nullable=False)
    description = Column(Text, nullable=True)
    evidence_reference = Column(String(240), nullable=True)
    assigned_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    reviewed_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    review_notes = Column(Text, nullable=True)
    resolution_notes = Column(Text, nullable=True)
    customer_confirmation_status = Column(String(24), nullable=True)
    customer_problem_notes = Column(Text, nullable=True)
    first_response_at = Column(DateTime, nullable=True)
    assigned_at = Column(DateTime, nullable=True)
    resolved_at = Column(DateTime, nullable=True)
    closed_at = Column(DateTime, nullable=True)
    customer_confirmed_at = Column(DateTime, nullable=True)
    customer_problem_reported_at = Column(DateTime, nullable=True)
    idempotency_key = Column(String(128), nullable=False, unique=True, index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class ServiceOrderWarrantyClaimEvent(Base):
    __tablename__ = "service_order_warranty_claim_events"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    claim_id = Column(Integer, ForeignKey("service_order_warranty_claims.id", ondelete="CASCADE"), nullable=False, index=True)
    event_type = Column(String(40), nullable=False, index=True)
    actor_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    source = Column(String(32), nullable=False)
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
