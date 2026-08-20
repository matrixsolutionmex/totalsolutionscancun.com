from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Float, ForeignKey, Integer

from app.database.connection import Base


class ServiceOrderTracking(Base):
    __tablename__ = "service_order_tracking"

    id = Column(Integer, primary_key=True, index=True)
    service_order_id = Column(Integer, ForeignKey("service_orders.id"), unique=True, nullable=False, index=True)
    technician_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    tracking_active = Column(Boolean, default=False, nullable=False, index=True)
    current_lat = Column(Float, nullable=True)
    current_lng = Column(Float, nullable=True)
    accuracy_m = Column(Float, nullable=True)
    consent_granted_at = Column(DateTime, nullable=True)
    started_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, nullable=True)
    stopped_at = Column(DateTime, nullable=True)
