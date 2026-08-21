from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, Numeric, String

from app.database.connection import Base


class PricingRate(Base):
    """Versioned platform pricing matrix; organization rows are optional overrides."""

    __tablename__ = "pricing_rates"

    id = Column(Integer, primary_key=True, index=True)
    organization_id = Column(Integer, ForeignKey("organizations.id"), nullable=True, index=True)
    country = Column(String(8), nullable=False, default="MX")
    city = Column(String(80), nullable=False, default="CANCUN", index=True)
    service_type = Column(String(60), nullable=False, index=True)
    segment = Column(String(30), nullable=False, default="RESIDENTIAL", index=True)
    pricing_zone = Column(String(20), nullable=False, default="Z0", index=True)
    visit_base_price = Column(Numeric(12, 2), nullable=False)
    travel_surcharge = Column(Numeric(12, 2), nullable=False, default=0)
    pricing_version = Column(String(40), nullable=False, default="CANCUN_V1")
    active = Column(Boolean, nullable=False, default=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=True)
