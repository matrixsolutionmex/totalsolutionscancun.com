from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint

from app.database.connection import Base


class ProfessionalApplicationEvent(Base):
    __tablename__ = "professional_application_events"

    id = Column(Integer, primary_key=True, index=True)
    application_id = Column(Integer, ForeignKey("professional_applications.id"), nullable=False, index=True)
    previous_status = Column(String(32), nullable=True)
    new_status = Column(String(32), nullable=False)
    actor_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    actor_role = Column(String(32), nullable=False)
    note = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)


class ProfessionalApplicationNote(Base):
    __tablename__ = "professional_application_notes"

    id = Column(Integer, primary_key=True, index=True)
    application_id = Column(Integer, ForeignKey("professional_applications.id"), nullable=False, index=True)
    actor_user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    note = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)


class ProfessionalApplicationSkill(Base):
    __tablename__ = "professional_application_skills"
    __table_args__ = (UniqueConstraint("application_id", "specialty", name="uq_professional_application_skill"),)

    id = Column(Integer, primary_key=True, index=True)
    application_id = Column(Integer, ForeignKey("professional_applications.id"), nullable=False, index=True)
    specialty = Column(String(100), nullable=False, index=True)
    declared = Column(Boolean, nullable=False, default=True)
    validation_status = Column(String(20), nullable=False, default="DECLARED", index=True)
    validated_by = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    validated_at = Column(DateTime, nullable=True)
    note = Column(Text, nullable=True)
