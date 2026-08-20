from datetime import datetime

from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from app.database.connection import Base


class ServiceProperty(Base):
    __tablename__ = "properties"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=True, index=True)
    lead_id = Column(Integer, ForeignKey("leads.id"), nullable=True, index=True)
    property_code = Column(String, unique=True, index=True, nullable=True)

    profile_type = Column(String, nullable=False, default="OUTRO")
    address_line1 = Column(String, nullable=False)
    address_line2 = Column(String, nullable=True)
    street_number = Column(String, nullable=True)
    district = Column(String, nullable=True)
    locality = Column(String, nullable=True)
    administrative_area = Column(String, nullable=True)
    country_code = Column(String, nullable=True, default="MX")
    postal_code = Column(String, nullable=True)
    google_maps_url = Column(String, nullable=True)
    latitude = Column(String, nullable=True)
    longitude = Column(String, nullable=True)
    location_lat = Column(Float, nullable=True)
    location_lng = Column(Float, nullable=True)
    location_accuracy_m = Column(Float, nullable=True)
    location_source = Column(String(20), nullable=True)
    location_confirmed_at = Column(DateTime, nullable=True)
    access_instructions = Column(String, nullable=True)
    extra_json = Column(String, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=True)

    lead = relationship("Lead")
    service_requests = relationship("ServiceRequest", back_populates="property_record")
    service_orders = relationship("ServiceOrder", back_populates="property_record")
