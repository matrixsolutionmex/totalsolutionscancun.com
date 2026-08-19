from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String

from app.database.connection import Base


class ReferralAttribution(Base):
    __tablename__ = "referral_attributions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, unique=True, index=True)
    source = Column(String, nullable=True)
    referral_code = Column(String, nullable=True, index=True)
    referral_email = Column(String, nullable=True)
    invitation_id = Column(Integer, ForeignKey("organization_invitations.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)

