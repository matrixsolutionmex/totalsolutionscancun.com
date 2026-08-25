from datetime import datetime

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, Numeric, String, Text

from app.database.connection import Base


class ServiceOpportunity(Base):
    """Sanitized, claimable marketplace opportunity separate from private CRM leads."""

    __tablename__ = "service_opportunities"

    id = Column(Integer, primary_key=True, index=True)
    public_id = Column(String, unique=True, nullable=False, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    marketplace_link_id = Column(Integer, ForeignKey("organization_marketplace_links.id"), nullable=True, index=True)
    source = Column(String, nullable=False, default="MARKETPLACE", index=True)
    service_type = Column(String, nullable=False)
    segment = Column(String, nullable=True, index=True)
    country = Column(String, nullable=True)
    state = Column(String, nullable=True)
    city = Column(String, nullable=True, index=True)
    approx_latitude = Column(Float, nullable=True)
    approx_longitude = Column(Float, nullable=True)
    urgency = Column(String, nullable=False, default="NORMAL", index=True)
    estimated_value_min = Column(Numeric(12, 2), nullable=True)
    estimated_value_max = Column(Numeric(12, 2), nullable=True)
    pricing_service_type = Column(String, nullable=True)
    pricing_zone = Column(String(20), nullable=True)
    visit_calculated_price = Column(Numeric(12, 2), nullable=True)
    market_reference_min = Column(Numeric(12, 2), nullable=True)
    market_reference_max = Column(Numeric(12, 2), nullable=True)
    customer_budget_min = Column(Numeric(12, 2), nullable=True)
    customer_budget_max = Column(Numeric(12, 2), nullable=True)
    pricing_currency = Column(String(8), nullable=True)
    pricing_version = Column(String(40), nullable=True)
    pricing_distance_km = Column(Float, nullable=True)
    pricing_duration_minutes = Column(Float, nullable=True)
    pricing_snapshot_json = Column(Text, nullable=True)
    requested_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    scheduled_for = Column(DateTime, nullable=True, index=True)
    description_public = Column(Text, nullable=True)
    status = Column(String, nullable=False, default="AVAILABLE", index=True)
    claimed_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    claimed_at = Column(DateTime, nullable=True)
    lead_id = Column(Integer, ForeignKey("leads.id"), nullable=True, index=True)
    service_order_id = Column(Integer, ForeignKey("service_orders.id"), nullable=True, index=True)
    service_request_id = Column(Integer, ForeignKey("service_requests.id"), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=True)
