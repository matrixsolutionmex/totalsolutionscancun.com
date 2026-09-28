from datetime import datetime

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Integer, String, UniqueConstraint

from app.database.connection import Base


class IdentityVerification(Base):
    __tablename__ = "identity_verifications"
    __table_args__ = (
        UniqueConstraint("user_id", "organization_id", name="uq_identity_verification_user_org"),
        CheckConstraint(
            "status IN ('NOT_STARTED', 'IN_PROGRESS', 'PENDING_REVIEW', 'NEEDS_ACTION', 'VERIFIED', 'REJECTED', 'EXPIRED', 'SUSPENDED')",
            name="ck_identity_verification_status",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=True, index=True)
    status = Column(String(32), nullable=False, default="NOT_STARTED", index=True)
    provider = Column(String(64), nullable=True)
    external_reference = Column(String(128), nullable=True)
    verified_at = Column(DateTime, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    next_action_code = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
