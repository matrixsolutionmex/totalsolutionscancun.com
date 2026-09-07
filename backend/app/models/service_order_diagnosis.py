from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, Text, UniqueConstraint
from sqlalchemy.orm import relationship

from app.database.connection import Base


class ServiceOrderDiagnosis(Base):
    __tablename__ = "service_order_diagnoses"
    __table_args__ = (UniqueConstraint("service_order_id", name="uq_service_order_diagnosis_order"),)

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    service_order_id = Column(Integer, ForeignKey("service_orders.id"), nullable=False, index=True)
    diagnosis_text = Column(Text, nullable=False, default="")
    problem_found = Column(Text, nullable=True)
    recommended_solution = Column(Text, nullable=True)
    observations = Column(Text, nullable=True)
    responsible_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)

    service_order = relationship("ServiceOrder")
    responsible_user = relationship("User", foreign_keys=[responsible_user_id])
