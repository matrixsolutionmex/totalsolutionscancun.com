from datetime import datetime

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Integer, String, UniqueConstraint

from app.database.connection import Base


HUMAN_REVIEW_DECISIONS = frozenset({"APPROVE", "REJECT", "REQUEST_CHANGES"})
HUMAN_REVIEW_REASON_CODES = frozenset({
    "PROVIDER_RESULT_REVIEW",
    "DOCUMENT_CLARIFICATION",
    "INSUFFICIENT_EVIDENCE",
    "POLICY_CHECK",
})


class IdentityHumanReviewDecision(Base):
    __tablename__ = "identity_human_review_decisions"
    __table_args__ = (
        UniqueConstraint("idempotency_key_hash", name="uq_identity_review_idempotency_hash"),
        CheckConstraint(
            "decision IN ('APPROVE', 'REJECT', 'REQUEST_CHANGES')",
            name="ck_identity_review_decision",
        ),
        CheckConstraint(
            "reason_code IN ('PROVIDER_RESULT_REVIEW', 'DOCUMENT_CLARIFICATION', 'INSUFFICIENT_EVIDENCE', 'POLICY_CHECK')",
            name="ck_identity_review_reason_code",
        ),
        CheckConstraint(
            "organization_id = reviewer_organization_id",
            name="ck_identity_review_tenant_match",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    attempt_id = Column(Integer, ForeignKey("identity_verification_attempts.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    reviewer_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    reviewer_organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False)
    decision = Column(String(32), nullable=False)
    reason_code = Column(String(64), nullable=False)
    idempotency_key_hash = Column(String(64), nullable=False, unique=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
