from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from app.database.connection import Base


class ContractEvent(Base):
    __tablename__ = "contract_events"

    id = Column(Integer, primary_key=True, index=True)

    contract_id = Column(Integer, ForeignKey("contracts.id"), nullable=False)
    actor_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    actor_name = Column(String)

    event_type = Column(String, nullable=False)
    message = Column(Text)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=True)