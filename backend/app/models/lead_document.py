from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String
from app.database.connection import Base


class LeadDocument(Base):
    __tablename__ = "lead_documents"

    id = Column(Integer, primary_key=True, index=True)

    lead_id = Column(Integer, ForeignKey("leads.id"), nullable=False)
    uploaded_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    document_type = Column(String, nullable=False)
    file_name = Column(String, nullable=False)
    file_path = Column(String, nullable=False)
    file_mime = Column(String)
    file_size = Column(Integer)

    created_at = Column(DateTime, default=datetime.utcnow, nullable=True)
