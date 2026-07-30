from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String

from app.database.connection import Base


class DeletionRequest(Base):
    __tablename__ = "deletion_requests"

    id = Column(Integer, primary_key=True, index=True)
    lead_id = Column(Integer, ForeignKey("leads.id"), nullable=False, index=True)
    document_id = Column(Integer, ForeignKey("lead_documents.id"), nullable=True, index=True)
    target_type = Column(String, nullable=False, default="DOCUMENT")
    reason = Column(String, nullable=False)
    status = Column(String, nullable=False, default="PENDENTE")
    requested_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    requested_by_role = Column(String, nullable=False)
    reviewed_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    decision_reason = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    reviewed_at = Column(DateTime, nullable=True)
