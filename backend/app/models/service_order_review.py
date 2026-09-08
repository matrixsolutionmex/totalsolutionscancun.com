from datetime import datetime
from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint

from app.database.connection import Base


class ServiceOrderReview(Base):
    """Immutable customer feedback record; quality aggregates are derived from this table."""

    __tablename__ = "service_order_reviews"
    __table_args__ = (
        UniqueConstraint("service_order_id", name="uq_service_order_review_order"),
        CheckConstraint("overall_rating BETWEEN 1 AND 5", name="ck_review_overall_rating"),
        CheckConstraint("service_quality_rating BETWEEN 1 AND 5", name="ck_review_service_quality_rating"),
        CheckConstraint("punctuality_rating BETWEEN 1 AND 5", name="ck_review_punctuality_rating"),
        CheckConstraint("communication_rating BETWEEN 1 AND 5", name="ck_review_communication_rating"),
        CheckConstraint("nps_score IS NULL OR nps_score BETWEEN 0 AND 10", name="ck_review_nps_score"),
    )

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=False, index=True)
    service_order_id = Column(Integer, ForeignKey("service_orders.id"), nullable=False, index=True)
    customer_lead_id = Column(Integer, ForeignKey("leads.id"), nullable=False, index=True)
    technician_user_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    overall_rating = Column(Integer, nullable=False)
    service_quality_rating = Column(Integer, nullable=False)
    punctuality_rating = Column(Integer, nullable=False)
    communication_rating = Column(Integer, nullable=False)
    nps_score = Column(Integer, nullable=True)
    comment = Column(Text, nullable=True)
    visibility = Column(String(16), nullable=False, default="VISIBLE")
    moderation_reason = Column(String(240), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)
