from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, UniqueConstraint

from app.database.connection import Base


class TechnicianSkill(Base):
    """Organization-scoped skill declarations used by internal recommendations."""

    __tablename__ = "technician_skills"
    __table_args__ = (
        UniqueConstraint("organization_id", "technician_user_id", "skill", name="uq_technician_skill"),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    technician_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    skill = Column(String(80), nullable=False, index=True)
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)

