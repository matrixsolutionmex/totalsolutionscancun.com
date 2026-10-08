from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint

from app.database.connection import Base


class CustomerPortalInvitation(Base):
    __tablename__ = "customer_portal_invitations"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_customer_portal_invitation_idempotency"),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    service_request_id = Column(Integer, ForeignKey("service_requests.id"), nullable=False, index=True)
    channel = Column(String(16), nullable=False)
    destination_hash = Column(String(64), nullable=False, index=True)
    masked_destination = Column(String(160), nullable=False)
    token_hash = Column(String(64), nullable=False, unique=True, index=True)
    language = Column(String(12), nullable=False, default="es")
    status = Column(String(24), nullable=False, default="QUEUED", index=True)
    expires_at = Column(DateTime, nullable=False)
    idempotency_key = Column(String(160), nullable=False)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    revoked_at = Column(DateTime, nullable=True)
    consumed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class CustomerPortalInvitationEvent(Base):
    __tablename__ = "customer_portal_invitation_events"

    id = Column(Integer, primary_key=True, index=True)
    invitation_id = Column(Integer, ForeignKey("customer_portal_invitations.id"), nullable=False, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    event_type = Column(String(40), nullable=False, index=True)
    actor_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    metadata_json = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
