from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text

from app.database.connection import Base


class ProfessionalApplication(Base):
    __tablename__ = "professional_applications"

    id = Column(Integer, primary_key=True, index=True)
    public_code = Column(String(32), nullable=False, unique=True, index=True)
    submission_key_hash = Column(String(64), nullable=True, unique=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=True, index=True)
    full_name = Column(String(160), nullable=False)
    email = Column(String(320), nullable=False, index=True)
    phone = Column(String(40), nullable=True)
    whatsapp = Column(String(40), nullable=False)
    city = Column(String(120), nullable=False)
    zone = Column(String(120), nullable=False)
    professional_type = Column(String(80), nullable=False)
    experience = Column(String(80), nullable=False)
    professional_level = Column(String(40), nullable=False)
    specialties_json = Column(Text, nullable=False)
    coverage_json = Column(Text, nullable=False)
    availability = Column(String(160), nullable=False)
    languages = Column(String(160), nullable=False)
    has_vehicle = Column(Boolean, nullable=False, default=False)
    has_tools = Column(Boolean, nullable=False, default=False)
    has_team = Column(Boolean, nullable=False, default=False)
    can_invoice = Column(Boolean, nullable=False, default=False)
    attention_emergencies = Column(Boolean, nullable=False, default=False)
    presentation = Column(Text, nullable=True)
    uploads_json = Column(Text, nullable=True)
    consent_data = Column(Boolean, nullable=False, default=False)
    consent_contact = Column(Boolean, nullable=False, default=False)
    consent_profile = Column(Boolean, nullable=False, default=False)
    status = Column(String(32), nullable=False, default="RECEIVED", index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
