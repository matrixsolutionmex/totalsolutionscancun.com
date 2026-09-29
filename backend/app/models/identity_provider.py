from datetime import datetime

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Integer, String, UniqueConstraint

from app.database.connection import Base


class IdentityVerificationAttempt(Base):
    __tablename__ = "identity_verification_attempts"
    __table_args__ = (
        UniqueConstraint("attempt_key_hash", name="uq_identity_attempt_key_hash"),
        CheckConstraint(
            "status IN ('CREATED', 'IN_PROGRESS', 'PENDING_REVIEW', 'REJECTED', 'EXPIRED')",
            name="ck_identity_attempt_status",
        ),
        CheckConstraint("provider IN ('metamap')", name="ck_identity_attempt_provider"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=True, index=True)
    identity_verification_id = Column(Integer, ForeignKey("identity_verifications.id"), nullable=False, index=True)
    attempt_key_hash = Column(String(64), nullable=False)
    provider = Column(String(64), nullable=False)
    provider_mode = Column(String(32), nullable=False)
    policy = Column(String(32), nullable=False)
    status = Column(String(32), nullable=False, default="CREATED", index=True)
    consented_at = Column(DateTime, nullable=False)
    consent_version = Column(String(64), nullable=False)
    expires_at = Column(DateTime, nullable=False)
    external_reference = Column(String(128), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)


class IdentityVerificationEvent(Base):
    __tablename__ = "identity_verification_events"
    __table_args__ = (UniqueConstraint("provider", "provider_event_id", name="uq_identity_provider_event"),)

    id = Column(Integer, primary_key=True, index=True)
    provider = Column(String(64), nullable=False)
    provider_event_id = Column(String(128), nullable=False)
    identity_verification_attempt_id = Column(Integer, ForeignKey("identity_verification_attempts.id"), nullable=True, index=True)
    event_name = Column(String(64), nullable=False)
    payload_hash = Column(String(64), nullable=False)
    status = Column(String(32), nullable=False, default="RECEIVED")
    reason_code = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    processed_at = Column(DateTime, nullable=True)
