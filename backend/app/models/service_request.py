from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import relationship

from app.database.connection import Base


class ServiceRequest(Base):
    __tablename__ = "service_requests"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=True, index=True)
    marketplace_link_id = Column(Integer, ForeignKey("organization_marketplace_links.id"), nullable=True, index=True)
    lead_id = Column(Integer, ForeignKey("leads.id"), nullable=False, index=True)
    property_id = Column(Integer, ForeignKey("properties.id"), nullable=True, index=True)

    tracking_token = Column(String, unique=True, index=True, nullable=False)
    idempotency_key = Column(String, index=True, nullable=True)
    source = Column(String, default="CLIENT_PORTAL", nullable=False)
    status = Column(String, default="SALES_QUEUE", nullable=False, index=True)

    service_category = Column(String, nullable=False)
    problem_description = Column(Text, nullable=True)
    urgency = Column(String, default="NORMAL", nullable=False)
    preferred_visit_at = Column(DateTime, nullable=True)
    access_instructions = Column(Text, nullable=True)
    location_lat = Column(Float, nullable=True)
    location_lng = Column(Float, nullable=True)
    location_accuracy_m = Column(Float, nullable=True)
    location_source = Column(String(20), nullable=True)
    location_confirmed_at = Column(DateTime, nullable=True)
    pricing_service_type = Column(String, nullable=True)
    pricing_segment = Column(String(30), nullable=True)
    pricing_zone = Column(String(20), nullable=True)
    visit_base_price = Column(Numeric(12, 2), nullable=True)
    travel_surcharge = Column(Numeric(12, 2), nullable=True)
    urgency_level = Column(String(20), nullable=True)
    urgency_multiplier = Column(Numeric(6, 3), nullable=True)
    visit_calculated_price = Column(Numeric(12, 2), nullable=True)
    market_reference_min = Column(Numeric(12, 2), nullable=True)
    market_reference_max = Column(Numeric(12, 2), nullable=True)
    customer_budget_min = Column(Numeric(12, 2), nullable=True)
    customer_budget_max = Column(Numeric(12, 2), nullable=True)
    final_service_price = Column(Numeric(12, 2), nullable=True)
    pricing_currency = Column(String(8), nullable=True)
    pricing_version = Column(String(40), nullable=True)
    visit_credit_policy = Column(String(80), nullable=True)
    pricing_distance_km = Column(Float, nullable=True)
    pricing_duration_minutes = Column(Float, nullable=True)
    pricing_snapshot_json = Column(Text, nullable=True)

    requester_name = Column(String, nullable=False)
    requester_phone = Column(String, nullable=True)
    requester_email = Column(String, nullable=True)
    public_language = Column(String, default="es-MX", nullable=True)
    consent_privacy = Column(Boolean, default=False, nullable=False)
    consent_images = Column(Boolean, default=False, nullable=False)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=True)

    lead = relationship("Lead")
    property_record = relationship("ServiceProperty", back_populates="service_requests")
    media = relationship("ServiceRequestMedia", back_populates="service_request", cascade="all, delete-orphan")
    service_order = relationship("ServiceOrder", back_populates="service_request", uselist=False)


class ServiceRequestMedia(Base):
    __tablename__ = "service_request_media"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=True, index=True)
    service_request_id = Column(Integer, ForeignKey("service_requests.id"), nullable=False, index=True)
    category = Column(String, default="EVIDENCIA_INICIAL", nullable=False)
    original_filename = Column(String, nullable=False)
    storage_path = Column(String, nullable=False)
    content_type = Column(String, nullable=False)
    size_bytes = Column(Integer, nullable=False)
    uploaded_at = Column(DateTime, default=datetime.utcnow, nullable=True)

    service_request = relationship("ServiceRequest", back_populates="media")
