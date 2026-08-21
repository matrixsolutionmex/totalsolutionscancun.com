from datetime import datetime

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, Numeric, String, Text
from sqlalchemy.orm import relationship

from app.database.connection import Base


TRACKING_STARTABLE_ORDER_STATUSES = frozenset({"ABERTA", "AGENDADA", "APROVADA", "ASSIGNED", "ACCEPTED"})


class ServiceOrder(Base):
    __tablename__ = "service_orders"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=True, index=True)
    order_number = Column(String, unique=True, index=True, nullable=True)

    lead_id = Column(Integer, ForeignKey("leads.id"), index=True, nullable=False)
    property_id = Column(String, index=True, nullable=True)
    property_record_id = Column(Integer, ForeignKey("properties.id"), nullable=True, index=True)
    service_request_id = Column(Integer, ForeignKey("service_requests.id"), unique=True, nullable=True, index=True)

    status = Column(String, default="ABERTA", nullable=False)
    warranty_days = Column(Integer, default=90, nullable=False)
    opened_at = Column(DateTime, default=datetime.utcnow, nullable=True)
    scheduled_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    responsible_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    supervisor_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    signature_status = Column(String, default="PENDENTE", nullable=False)
    qr_token = Column(String, nullable=True)
    warranty_seal_status = Column(String, default="PENDENTE", nullable=False)
    checklist_status = Column(String, default="PENDENTE", nullable=False)
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

    created_at = Column(DateTime, default=datetime.utcnow, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=True)

    lead = relationship("Lead", back_populates="service_orders")
    property_record = relationship("ServiceProperty", back_populates="service_orders")
    service_request = relationship("ServiceRequest", back_populates="service_order")
    responsible_user = relationship("User", foreign_keys=[responsible_user_id])
    tracking = relationship("ServiceOrderTracking", back_populates="service_order", uselist=False)

    @property
    def tracking_active(self):
        return bool(self.tracking and self.tracking.tracking_active)

    @property
    def tracking_start_allowed(self):
        return not self.tracking_active and (self.status or "").strip().upper() in TRACKING_STARTABLE_ORDER_STATUSES
