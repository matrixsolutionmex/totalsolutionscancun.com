from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint

from app.database.connection import Base


MEMBERSHIP_TYPES = frozenset({"OWNER", "ADMIN", "TECHNICIAN", "NETWORK_PARTNER", "NETWORK_OPERATOR", "FRANCHISE_ADMIN"})
MEMBERSHIP_STATUSES = frozenset({"PENDING", "ACTIVE", "EXIT_REQUESTED", "TRANSFER_PENDING", "EXITED", "SUSPENDED", "REJECTED"})


class OrganizationMembership(Base):
    """A non-destructive organization relationship during the tenant migration."""

    __tablename__ = "organization_memberships"
    __table_args__ = (
        UniqueConstraint("user_id", "organization_id", name="uq_organization_membership_user_org"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    membership_type = Column(String(32), nullable=False, default="TECHNICIAN")
    role = Column(String(32), nullable=False, default="TECHNICIAN")
    status = Column(String(32), nullable=False, default="PENDING", index=True)
    supervisor_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    is_primary = Column(Boolean, nullable=False, default=False, index=True)
    is_operational = Column(Boolean, nullable=False, default=False, index=True)
    joined_at = Column(DateTime, nullable=True)
    approved_at = Column(DateTime, nullable=True)
    approved_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    exited_at = Column(DateTime, nullable=True)
    exit_reason = Column(String(240), nullable=True)
    requested_at = Column(DateTime, nullable=True)
    requested_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    terms_version = Column(String(64), nullable=True)
    terms_accepted_at = Column(DateTime, nullable=True)
    terms_acceptance_ip_hash = Column(String(128), nullable=True)
    terms_acceptance_metadata_json = Column(Text, nullable=True)
    fee_policy_id = Column(Integer, ForeignKey("network_fee_policies.id"), nullable=True, index=True)
    territory_country = Column(String(8), nullable=True)
    territory_state = Column(String(80), nullable=True)
    territory_city = Column(String(120), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


Index(
    "uq_organization_memberships_primary_active_user",
    OrganizationMembership.user_id,
    unique=True,
    postgresql_where=(OrganizationMembership.status == "ACTIVE") & (OrganizationMembership.is_primary.is_(True)),
    sqlite_where=(OrganizationMembership.status == "ACTIVE") & (OrganizationMembership.is_primary.is_(True)),
)


class NetworkFeePolicy(Base):
    """Configuration foundation only; no financial calculation is performed here."""

    __tablename__ = "network_fee_policies"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=True, index=True)
    network_id = Column(Integer, nullable=True, index=True)
    country = Column(String(8), nullable=False)
    region = Column(String(120), nullable=True)
    service_origin = Column(String(32), nullable=False, default="NETWORK")
    platform_fee_rate = Column(String(32), nullable=True)
    tax_policy = Column(String(64), nullable=True)
    currency = Column(String(8), nullable=False, default="MXN")
    effective_from = Column(DateTime, nullable=True)
    effective_until = Column(DateTime, nullable=True)
    status = Column(String(24), nullable=False, default="DRAFT", index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
