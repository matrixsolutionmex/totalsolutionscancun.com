from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, UniqueConstraint

from app.database.connection import Base


class OrganizationMarketplaceLink(Base):
    """Public, organization-owned entry point for marketplace attribution."""

    __tablename__ = "organization_marketplace_links"
    __table_args__ = (UniqueConstraint("organization_id", "slug", name="uq_marketplace_link_org_slug"),)

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    name = Column(String(160), nullable=False)
    slug = Column(String(120), nullable=False)
    service_category = Column(String(120), nullable=True)
    campaign_name = Column(String(160), nullable=True)
    source_code = Column(String(80), nullable=False, default="MARKETPLACE_LINK")
    visibility_scope = Column(String(20), nullable=False, default="ORGANIZATION")
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
