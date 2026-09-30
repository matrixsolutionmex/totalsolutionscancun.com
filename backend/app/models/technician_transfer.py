from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text

from app.database.connection import Base


TRANSFER_TYPES = frozenset({"EXCLUSIVE_TRANSFER", "NETWORK_MEMBERSHIP"})
TRANSFER_STATUSES = frozenset({"TRANSFER_REQUESTED", "TRANSFER_PENDING_BLOCKED", "APPROVED", "REJECTED", "CANCELLED"})


class TechnicianTransferRequest(Base):
    __tablename__ = "technician_transfer_requests"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    from_organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    to_organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    status = Column(String(40), nullable=False, default="TRANSFER_REQUESTED", index=True)
    requested_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    reviewed_at = Column(DateTime, nullable=True)
    reviewed_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    reason = Column(Text, nullable=True)
    review_notes = Column(Text, nullable=True)
    terms_version = Column(String(64), nullable=True)
    terms_accepted_at = Column(DateTime, nullable=True)
    requested_transfer_type = Column(String(32), nullable=False, default="NETWORK_MEMBERSHIP")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
