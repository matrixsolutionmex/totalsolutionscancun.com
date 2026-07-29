from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String

from app.database.connection import Base


class LeadEvent(Base):
    __tablename__ = "lead_events"

    id = Column(Integer, primary_key=True, index=True)
    lead_id = Column(Integer, index=True, nullable=False)
    actor_id = Column(Integer, nullable=True)
    actor_name = Column(String, nullable=True)
    event_type = Column(String, nullable=False)
    message = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
