import json
from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, Numeric, String, Text, UniqueConstraint

from app.database.connection import Base


class CampaignContact(Base):
    __tablename__ = "campaign_contacts"
    __table_args__ = (
        UniqueConstraint("organization_id", "normalized_email", name="uq_campaign_contact_org_email"),
        Index("ix_campaign_contacts_org_segment_score", "organization_id", "segment", "fit_score"),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    email = Column(String(320), nullable=False)
    normalized_email = Column(String(320), nullable=False)
    name = Column(String(160), nullable=True)
    company = Column(String(240), nullable=True)
    segment = Column(String(32), nullable=False, default="OTRO", index=True)
    source = Column(String(32), nullable=False, default="MANUAL")
    country = Column(String(8), nullable=True)
    state = Column(String(80), nullable=True)
    city = Column(String(120), nullable=True)
    language = Column(String(16), nullable=False, default="es")
    status = Column(String(24), nullable=False, default="ACTIVE", index=True)
    fit_score = Column(Integer, nullable=False, default=0)
    fit_score_breakdown = Column(Text, nullable=False, default="{}")
    fit_score_version = Column(String(24), nullable=False, default="v1")
    fit_score_calculated_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    def breakdown(self) -> dict:
        try:
            return json.loads(self.fit_score_breakdown or "{}")
        except (TypeError, ValueError):
            return {}


class CampaignSuppression(Base):
    __tablename__ = "campaign_suppressions"
    __table_args__ = (
        UniqueConstraint("organization_id", "normalized_email", name="uq_campaign_suppression_org_email"),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    normalized_email = Column(String(320), nullable=False)
    reason = Column(String(32), nullable=False)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class TechnicianReferral(Base):
    __tablename__ = "technician_referrals"
    __table_args__ = (
        UniqueConstraint("referral_code", name="uq_technician_referral_code"),
        Index("ix_technician_referrals_org_status", "organization_id", "status"),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    referrer_type = Column(String(24), nullable=False)
    referrer_id = Column(Integer, nullable=False, index=True)
    referral_code = Column(String(32), nullable=False)
    referred_name = Column(String(160), nullable=True)
    referred_phone = Column(String(40), nullable=True)
    referred_email = Column(String(320), nullable=True)
    normalized_phone = Column(String(40), nullable=True)
    normalized_email = Column(String(320), nullable=True)
    referred_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    referred_technician_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    source = Column(String(32), nullable=False, default="REFERRAL")
    status = Column(String(32), nullable=False, default="PENDING", index=True)
    approved_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    rejected_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    rejection_reason = Column(String(240), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    registered_at = Column(DateTime, nullable=True)
    approved_at = Column(DateTime, nullable=True)
    rejected_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class ReferralReward(Base):
    __tablename__ = "referral_rewards"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    referral_id = Column(Integer, ForeignKey("technician_referrals.id"), nullable=False, unique=True, index=True)
    reward_type = Column(String(32), nullable=False, default="PERCENT_DISCOUNT")
    reward_value = Column(Numeric(8, 2), nullable=False, default=50)
    reward_cap_amount = Column(Numeric(12, 2), nullable=False, default=1000)
    currency = Column(String(8), nullable=False, default="MXN")
    status = Column(String(32), nullable=False, default="REWARD_AVAILABLE", index=True)
    reserved_service_order_id = Column(Integer, ForeignKey("service_orders.id"), nullable=True)
    reserved_at = Column(DateTime, nullable=True)
    used_service_order_id = Column(Integer, ForeignKey("service_orders.id"), nullable=True)
    used_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class SegmentationReferralAuditEvent(Base):
    __tablename__ = "segmentation_referral_audit_events"
    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    actor_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    event_type = Column(String(48), nullable=False, index=True)
    contact_id = Column(Integer, ForeignKey("campaign_contacts.id"), nullable=True, index=True)
    referral_id = Column(Integer, ForeignKey("technician_referrals.id"), nullable=True, index=True)
    reward_id = Column(Integer, ForeignKey("referral_rewards.id"), nullable=True, index=True)
    service_order_id = Column(Integer, ForeignKey("service_orders.id"), nullable=True, index=True)
    previous_status = Column(String(32), nullable=True)
    new_status = Column(String(32), nullable=True)
    metadata_json = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
