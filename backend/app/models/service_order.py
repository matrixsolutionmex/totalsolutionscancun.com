from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import relationship

from app.database.connection import Base


class ServiceOrder(Base):
    __tablename__ = "service_orders"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=True, index=True)
    order_number = Column(String, unique=True, index=True, nullable=True)

    lead_id = Column(Integer, ForeignKey("leads.id"), unique=True, nullable=False)
    property_id = Column(String, index=True, nullable=True)

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

    created_at = Column(DateTime, default=datetime.utcnow, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=True)

    lead = relationship("Lead", back_populates="service_order")
