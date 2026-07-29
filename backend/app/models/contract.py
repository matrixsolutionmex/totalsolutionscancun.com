from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from app.database.connection import Base


class Contract(Base):
    __tablename__ = "contracts"

    id = Column(Integer, primary_key=True, index=True)

    lead_id = Column(Integer, ForeignKey("leads.id"), nullable=False)
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    contract_type = Column(String, nullable=False)
    status = Column(String, default="RASCUNHO")

    client_name = Column(String)
    client_email = Column(String)
    client_phone = Column(String)

    property_address = Column(String)
    business_value = Column(String)
    commission_value = Column(String)

    notes = Column(Text)
    html_content = Column(Text)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=True)