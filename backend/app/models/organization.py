from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Integer, String

from app.database.connection import Base


class Organization(Base):
    __tablename__ = "organizations"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    slug = Column(String, unique=True, index=True, nullable=False)
    industry_profile = Column(String, default="SERVICIOS_TECNICOS", nullable=False)
    country = Column(String, default="MX", nullable=False)
    language = Column(String, default="es-MX", nullable=False)
    currency = Column(String, default="MXN", nullable=False)
    timezone = Column(String, default="America/Cancun", nullable=False)
    date_format = Column(String, default="DD/MM/YYYY", nullable=False)
    plan = Column(String, default="INTERNAL", nullable=False)
    status = Column(String, default="ACTIVE", nullable=False)
    user_limit = Column(Integer, default=25, nullable=False)
    technician_limit = Column(Integer, default=20, nullable=False)
    monthly_service_limit = Column(Integer, default=1000, nullable=False)
    storage_limit_mb = Column(Integer, default=10240, nullable=False)
    is_platform_owner = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False)
