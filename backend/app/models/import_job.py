from datetime import datetime

from sqlalchemy import Column, DateTime, Integer, String, Text, UniqueConstraint

from app.database.connection import Base


class ImportJob(Base):
    __tablename__ = "import_jobs"
    __table_args__ = (
        UniqueConstraint("source", "batch_id", name="uq_import_jobs_source_batch"),
    )

    id = Column(Integer, primary_key=True, index=True)
    source = Column(String, nullable=False, index=True)
    batch_id = Column(String, nullable=False, index=True)
    status = Column(String, nullable=False, default="RECEIVED")
    total_received = Column(Integer, nullable=False, default=0)
    inserted = Column(Integer, nullable=False, default=0)
    skipped_duplicates = Column(Integer, nullable=False, default=0)
    invalid = Column(Integer, nullable=False, default=0)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    started_at = Column(DateTime, nullable=True)
    finished_at = Column(DateTime, nullable=True)
