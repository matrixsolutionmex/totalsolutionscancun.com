from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, UniqueConstraint

from app.database.connection import Base


class CustomerServiceLink(Base):
    __tablename__ = "customer_service_links"
    __table_args__ = (
        UniqueConstraint("customer_user_id", "service_request_id", name="uq_customer_service_link"),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    customer_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    service_request_id = Column(Integer, ForeignKey("service_requests.id"), nullable=False, index=True)
    verification_method = Column(String(32), nullable=False)
    verified_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    active = Column(Boolean, nullable=False, default=True, index=True)
    revoked_at = Column(DateTime, nullable=True)


class CustomerClaimToken(Base):
    __tablename__ = "customer_claim_tokens"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_customer_claim_token_hash"),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    service_request_id = Column(Integer, ForeignKey("service_requests.id"), nullable=False, index=True)
    token_hash = Column(String(64), nullable=False, index=True)
    channel = Column(String(16), nullable=False)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    used_at = Column(DateTime, nullable=True)
    revoked_at = Column(DateTime, nullable=True)
