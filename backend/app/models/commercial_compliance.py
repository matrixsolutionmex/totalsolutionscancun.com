from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint

from app.database.connection import Base


class GlobalSuppression(Base):
    __tablename__ = "global_suppressions"
    __table_args__ = (
        UniqueConstraint("organization_id", "normalized_email", name="uq_global_suppression_org_email"),
        UniqueConstraint("organization_id", "domain", name="uq_global_suppression_org_domain"),
        Index("ix_global_suppressions_scope_reason", "scope", "reason"),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=True, index=True)
    normalized_email = Column(String(320), nullable=True, index=True)
    domain = Column(String(255), nullable=True, index=True)
    reason = Column(String(40), nullable=False, index=True)
    scope = Column(String(40), nullable=False, default="EMAIL_ONLY", index=True)
    source = Column(String(80), nullable=False, default="MANUAL")
    notes = Column(Text, nullable=True)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class CommercialOutreach(Base):
    __tablename__ = "commercial_outreach"
    __table_args__ = (
        Index("ix_commercial_outreach_org_status", "organization_id", "status"),
        Index("ix_commercial_outreach_org_domain", "organization_id", "domain"),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    company_name = Column(String(240), nullable=False)
    domain = Column(String(255), nullable=True, index=True)
    recipient = Column(String(320), nullable=False, index=True)
    channel = Column(String(40), nullable=False, default="EMAIL")
    campaign_type = Column(String(80), nullable=False, default="HUMAN_OUTREACH")
    message_version = Column(String(80), nullable=True)
    privacy_notice_version = Column(String(80), nullable=False)
    contact_source = Column(String(120), nullable=False)
    contact_source_url = Column(Text, nullable=True)
    compliance_status = Column(String(40), nullable=False, default="PENDING_REVIEW", index=True)
    status = Column(String(40), nullable=False, default="DRAFT", index=True)
    human_approved_by = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    human_approved_at = Column(DateTime, nullable=True)
    contacted_at = Column(DateTime, nullable=True)
    response_status = Column(String(40), nullable=True)
    suppression_checked_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class CommercialAuditEvent(Base):
    __tablename__ = "commercial_audit_events"
    __table_args__ = (
        Index("ix_commercial_audit_org_type", "organization_id", "event_type"),
        Index("ix_commercial_audit_object", "object_type", "object_id"),
    )

    id = Column(Integer, primary_key=True, index=True)
    event_type = Column(String(80), nullable=False, index=True)
    actor_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=True, index=True)
    object_type = Column(String(80), nullable=True)
    object_id = Column(Integer, nullable=True)
    reason = Column(String(240), nullable=True)
    metadata_json = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
